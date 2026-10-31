"""Admin wallets: the prepaid balances that pay for API orders. Charges, refunds, funds, adjustments, keys, alerts.

Every staff user has exactly one AdminWallet and one personal API key. Every
balance change locks the wallet row (select_for_update), moves the balance with
a conditional F() update and writes one AdminWalletTransaction in the same
transaction, so the balance always equals its ledger (checked daily by
reconcile_wallets). Amounts are INR with 2 decimals; the charge is the order
value ROUND_HALF_UP, the same figure /api/v2 `status` reports as `charge`.
Worker wallets (wallet.Wallet) are never touched here.
"""

import logging
from datetime import timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

from common.notifications import rupees, send_admin_alert
from tasks.models import TaskClaim

from .api_keys import new_api_key, owner_may_use_api
from .models import AdminWallet, AdminWalletTransaction, Order

logger = logging.getLogger(__name__)

CENTS = Decimal("0.01")
# The largest balance a Decimal(14, 2) column holds.
MAX_BALANCE = Decimal("999999999999.99")
NOT_ENOUGH_FUNDS = "Not enough funds on balance"
CHARGE_TOO_SMALL = "The order amount is below ₹0.01. Increase the quantity."
INVALID_KEY = "Invalid API key"
NOT_STAFF = "Admin wallets belong to staff accounts only."
REFUSED_ALERT_INTERVAL = timedelta(minutes=15)
# Final statuses that give money back: Partial (remains) and Cancelled (all of it).
REFUND_STATUSES = (Order.FulfillmentStatus.PARTIAL, Order.FulfillmentStatus.CANCELLED)

Type = AdminWalletTransaction.Type
Direction = AdminWalletTransaction.Direction


class InsufficientFunds(ValidationError):
    """The wallet's balance does not cover the order's charge; nothing was created."""

    def __init__(self):
        super().__init__(NOT_ENOUGH_FUNDS)


# ---------------------------------------------------------------------------
# Amounts
# ---------------------------------------------------------------------------

def charge_for(order_value):
    """What an order costs: its value (rate x quantity / 1,000) ROUND_HALF_UP to 2 dp."""
    return Decimal(order_value).quantize(CENTS, rounding=ROUND_HALF_UP)


def reported_charge(order):
    """The `charge` /api/v2 reports: the amount debited, or for an order placed
    before prepaid balances, its value rounded by the same rule."""
    return order.charge_amount if order.charge_amount is not None else charge_for(order.total_order_value)


def delivered_units(order_id):
    return TaskClaim.objects.filter(task__order_id=order_id, status=TaskClaim.Status.APPROVED).count()


def refund_due(order, delivered):
    """What a final API order gives back; Decimal("0") when nothing is owed.

    Cancelled: the whole charge. Partial: charge x remains / quantity, rounded
    down to 2 dp, so a refund never exceeds what was charged.
    """
    if order.charge_amount is None or order.fulfillment_status not in REFUND_STATUSES:
        return Decimal("0")
    if order.fulfillment_status == Order.FulfillmentStatus.CANCELLED:
        return order.charge_amount
    remains = max(0, order.quantity - delivered)
    refund = (order.charge_amount * remains / order.quantity).quantize(CENTS, rounding=ROUND_DOWN)
    return min(refund, order.charge_amount)


def parse_amount(value, *, field="amount", allow_negative=False, allow_zero=False):
    """A request amount as an exact Decimal with at most 2 decimals, or ValidationError."""
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValidationError({field: "Enter an amount in rupees, for example 500.00."})
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        raise ValidationError({field: "Enter an amount in rupees, for example 500.00."})
    if not amount.is_finite() or amount != amount.quantize(CENTS):
        raise ValidationError({field: "Use at most 2 decimal places."})
    if amount < 0 and not allow_negative:
        raise ValidationError({field: "Enter 0 or more." if allow_zero else "Enter an amount greater than zero."})
    if amount == 0 and not allow_zero:
        raise ValidationError({field: "Enter a non-zero amount." if allow_negative else "Enter an amount greater than zero."})
    if abs(amount) > MAX_BALANCE:
        raise ValidationError({field: "The amount is too large."})
    return amount.quantize(CENTS)


def _text(value, *, field, max_length, required=False, label="text"):
    text = value.strip() if isinstance(value, str) else ("" if value is None else None)
    if text is None:
        raise ValidationError({field: f"The {label} must be text."})
    if required and not text:
        raise ValidationError({field: f"Enter the {label}."})
    if len(text) > max_length:
        raise ValidationError({field: f"The {label} can be at most {max_length} characters."})
    return text


# ---------------------------------------------------------------------------
# Wallets
# ---------------------------------------------------------------------------

def ensure_admin_wallet(user):
    """The staff user's wallet, created on first use (balance ₹0.00, no key yet)."""
    if not user.is_staff:
        raise ValidationError(NOT_STAFF)
    # Balance 0 is below the default threshold: nothing to announce until it has been above it.
    wallet, _ = AdminWallet.objects.get_or_create(admin=user, defaults={"low_balance_alerted": True})
    return wallet


def _locked(wallet_id):
    try:
        return AdminWallet.objects.select_for_update().select_related("admin").get(pk=wallet_id)
    except AdminWallet.DoesNotExist:
        raise ValidationError("Admin wallet not found.")


def _label(wallet):
    return f"Admin wallet #{wallet.pk} (admin #{wallet.admin_id})"


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------

def _post(wallet, *, transaction_type, direction, amount, order=None, reference="", note="", by=None, request_key=None):
    """Move the locked `wallet`'s balance by `amount` and write its ledger row.

    Runs inside the caller's transaction. A debit is a conditional update, so
    the balance can never go below zero whatever else is running.
    """
    if direction == Direction.DEBIT:
        if not AdminWallet.objects.filter(pk=wallet.pk, balance__gte=amount).update(balance=F("balance") - amount):
            raise InsufficientFunds()
    else:
        if wallet.balance + amount > MAX_BALANCE:
            raise ValidationError("The balance would exceed the largest amount that can be stored.")
        AdminWallet.objects.filter(pk=wallet.pk).update(balance=F("balance") + amount)
    wallet.balance = AdminWallet.objects.values_list("balance", flat=True).get(pk=wallet.pk)
    row = AdminWalletTransaction.objects.create(
        wallet=wallet, transaction_type=transaction_type, direction=direction, amount=amount,
        balance_after=wallet.balance, order=order,
        charge_order=order if transaction_type == Type.ORDER_CHARGE else None,
        refund_order=order if transaction_type == Type.REFUND else None,
        reference=reference, note=note, created_by=by, request_key=request_key,
    )
    _low_balance_alert(wallet)
    return row


def _low_balance_alert(wallet):
    """Announce the balance falling below the threshold once per crossing; re-arm when it is back above."""
    below = wallet.balance < wallet.low_balance_threshold
    if below == wallet.low_balance_alerted:
        return
    AdminWallet.objects.filter(pk=wallet.pk).update(low_balance_alerted=below)
    wallet.low_balance_alerted = below
    if below:
        send_admin_alert("api_low_balance", (
            f"{_label(wallet)}: balance {rupees(wallet.balance)} is below its low-balance threshold "
            f"of {rupees(wallet.low_balance_threshold)}. Add funds so its API orders keep being accepted."
        ))


def lock_wallet_for_order(wallet_id):
    """The wallet row, locked for this order's charge; refused if its owner stopped being active staff meanwhile."""
    wallet = _locked(wallet_id)
    if not owner_may_use_api(wallet.admin) or not wallet.api_key_hash:
        raise ValidationError(INVALID_KEY)
    return wallet


def check_order_funds(wallet, charge):
    if charge < CENTS:
        raise ValidationError(CHARGE_TOO_SMALL)
    if wallet.balance < charge:
        raise InsufficientFunds()


def debit_order_charge(wallet, order):
    """The order_charge row for a just-created order (inside the order's transaction)."""
    return _post(wallet, transaction_type=Type.ORDER_CHARGE, direction=Direction.DEBIT, amount=order.charge_amount,
                 order=order, note=f"Order #{order.pk}: {order.quantity} unit(s)")


def refund_api_order(order_id):
    """Credit what a Partial or Cancelled API order is owed, exactly once.

    The one refund path: called by the admin cancel flow and the order-expiry
    sweep (tasks.services._cancel_task_records) and by bulk delete, right after
    the order's final status is written, inside that same transaction.
    Idempotent: a second call (or a racing one) finds the refund row, which is
    also a unique column. Returns the refund row, or None when nothing is owed
    (a manual order, an order placed before prepaid balances, or a refund that
    rounds down to ₹0.00).
    """
    order = Order.objects.select_for_update().filter(pk=order_id).first()
    if order is None or order.admin_wallet_id is None or order.charge_amount is None:
        return None
    if order.fulfillment_status not in REFUND_STATUSES:
        return None
    existing = AdminWalletTransaction.objects.filter(refund_order=order).first()
    if existing:
        return existing
    delivered = delivered_units(order.pk)
    amount = refund_due(order, delivered)
    if amount <= 0:
        return None
    wallet = _locked(order.admin_wallet_id)
    remains = order.quantity if order.fulfillment_status == Order.FulfillmentStatus.CANCELLED else order.quantity - delivered
    try:
        with transaction.atomic():
            return _post(wallet, transaction_type=Type.REFUND, direction=Direction.CREDIT, amount=amount, order=order,
                         note=f"Order #{order.pk} {order.get_fulfillment_status_display().lower()}: "
                              f"{remains} of {order.quantity} unit(s) not delivered")
    except IntegrityError:
        # Refunded by a concurrent call; the savepoint undid this attempt's credit.
        return AdminWalletTransaction.objects.filter(refund_order=order).first()


def _recorded_request(request_key, *, wallet_id, transaction_type, amount, direction):
    """The row an earlier submit of this form wrote, or None; refuses a reused key with other details."""
    if request_key is None:
        return None
    existing = AdminWalletTransaction.objects.filter(request_key=request_key).first()
    if existing is None:
        return None
    same = (existing.wallet_id == wallet_id and existing.transaction_type == transaction_type
            and existing.amount == amount and existing.direction == direction)
    if not same:
        raise ValidationError("This request key was already used for a different entry. Reload the page and try again.")
    return existing


def _record_once(request_key, write, **match):
    """Run `write` unless this request key was recorded already; returns (row, created)."""
    existing = _recorded_request(request_key, **match)
    if existing:
        return existing, False
    try:
        with transaction.atomic():
            return write(), True
    except IntegrityError:
        existing = _recorded_request(request_key, **match)
        if existing:
            return existing, False
        raise


@transaction.atomic
def record_add_funds(*, wallet_id, amount, reference, note="", by, request_key=None):
    """Funds the admin paid in (UPI reference required). Returns (row, created)."""
    amount = parse_amount(amount)
    reference = _text(reference, field="reference", max_length=100, required=True, label="UPI reference")
    note = _text(note, field="note", max_length=500, label="note")

    def write():
        wallet = _locked(wallet_id)
        row = _post(wallet, transaction_type=Type.ADD_FUNDS, direction=Direction.CREDIT, amount=amount,
                    reference=reference, note=note, by=by, request_key=request_key)
        send_admin_alert("api_deposit", (
            f"Funds of {rupees(amount)} added to {_label(wallet).lower()}"
            f"{f' by admin #{by.pk}' if by is not None else ''}. New balance {rupees(wallet.balance)}."
        ))
        return row

    return _record_once(request_key, write, wallet_id=wallet_id, transaction_type=Type.ADD_FUNDS, amount=amount,
                        direction=Direction.CREDIT)


@transaction.atomic
def record_adjustment(*, wallet_id, amount, note, by, request_key=None):
    """A superuser's manual correction: `amount` > 0 adds, < 0 takes away (never below ₹0.00).

    The note is required; the change is also written to the wallet owner's
    audit log (accounts.UserChangeLog). Returns (row, created).
    """
    from accounts.models import UserChangeLog

    signed = parse_amount(amount, allow_negative=True)
    note = _text(note, field="note", max_length=500, required=True, label="note")
    direction = Direction.CREDIT if signed > 0 else Direction.DEBIT
    value = abs(signed)

    def write():
        wallet = _locked(wallet_id)
        before = wallet.balance
        if direction == Direction.DEBIT and wallet.balance < value:
            raise ValidationError({"amount": f"The adjustment would take the balance below ₹0.00 (current balance {rupees(wallet.balance)})."})
        row = _post(wallet, transaction_type=Type.ADJUSTMENT, direction=direction, amount=value, note=note, by=by,
                    request_key=request_key)
        UserChangeLog.objects.create(
            user_id=wallet.admin_id, field="admin_wallet_adjustment", changed_by=by,
            old_value=format(before, "f"), new_value=f"{format(wallet.balance, 'f')} ({'+' if direction == Direction.CREDIT else '-'}"
                                                     f"{format(value, 'f')}: {note})"[:2000],
        )
        return row

    return _record_once(request_key, write, wallet_id=wallet_id, transaction_type=Type.ADJUSTMENT, amount=value,
                        direction=direction)


# ---------------------------------------------------------------------------
# Keys and settings
# ---------------------------------------------------------------------------

@transaction.atomic
def regenerate_api_key(*, wallet_id):
    """Replace the wallet's key (or create its first one); the old key stops working at once.

    Returns (wallet, new key); the key is never stored or shown again.
    """
    wallet = _locked(wallet_id)
    if not wallet.admin.is_staff:
        raise ValidationError(NOT_STAFF)
    key, prefix, digest = new_api_key()
    now = timezone.now()
    AdminWallet.objects.filter(pk=wallet.pk).update(api_key_prefix=prefix, api_key_hash=digest, api_key_created_at=now)
    wallet.api_key_prefix, wallet.api_key_hash, wallet.api_key_created_at = prefix, digest, now
    return wallet, key


@transaction.atomic
def update_low_balance_threshold(*, wallet_id, low_balance_threshold):
    wallet = _locked(wallet_id)
    threshold = parse_amount(low_balance_threshold, field="low_balance_threshold", allow_zero=True)
    AdminWallet.objects.filter(pk=wallet.pk).update(low_balance_threshold=threshold)
    wallet.low_balance_threshold = threshold
    _low_balance_alert(wallet)
    return wallet


# ---------------------------------------------------------------------------
# Orders refused for insufficient funds: batched alerts
# ---------------------------------------------------------------------------

def record_refused_order(wallet_id, now=None):
    """Count an order refused for insufficient funds and announce it, at most once per 15 minutes per wallet.

    Runs after the refused order's transaction rolled back. Never raises: the
    API reply must not depend on the alert bookkeeping.
    """
    from tasks.services import _retry_when_locked  # here, not at the top: tasks.services imports this module

    try:
        _retry_when_locked(_count_refused_order, wallet_id=wallet_id)
        flush_refused_order_alerts(now=now, wallet_id=wallet_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Refused-order alert bookkeeping failed: %s", type(exc).__name__)


def _count_refused_order(*, wallet_id):
    AdminWallet.objects.filter(pk=wallet_id).update(refused_orders_unreported=F("refused_orders_unreported") + 1)


def flush_refused_order_alerts(now=None, wallet_id=None):
    """Announce counted refusals for wallets whose last announcement is 15+ minutes old. Returns how many sent.

    Called on each refusal and by the expire_claims cron, so refusals that
    arrive inside the 15 minutes are summarised once the interval has passed.
    """
    now = now or timezone.now()
    due = Q(refused_orders_unreported__gt=0) & (Q(refused_alert_at__isnull=True) | Q(refused_alert_at__lte=now - REFUSED_ALERT_INTERVAL))
    wallets = AdminWallet.objects.filter(due)
    if wallet_id is not None:
        wallets = wallets.filter(pk=wallet_id)
    sent = 0
    for wallet in wallets.values("pk", "admin_id", "refused_orders_unreported", "refused_alert_at", "balance"):
        count = wallet["refused_orders_unreported"]
        # A conditional update: exactly one process announces each batch.
        if not AdminWallet.objects.filter(due, pk=wallet["pk"]).update(
            refused_orders_unreported=F("refused_orders_unreported") - count, refused_alert_at=now,
        ):
            continue
        since = f" since {timezone.localtime(wallet['refused_alert_at']):%d %b %H:%M} IST" if wallet["refused_alert_at"] else ""
        send_admin_alert("api_insufficient_funds", (
            f"Admin wallet #{wallet['pk']} (admin #{wallet['admin_id']}): {count} order(s) refused for insufficient funds{since}. "
            f"Balance {rupees(wallet['balance'])}."
        ))
        sent += 1
    return sent
