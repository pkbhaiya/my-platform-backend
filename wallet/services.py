import re
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from common.models import SiteConfiguration
from common.notifications import rupees, send_admin_alert
from tasks.models import TaskClaim

from .models import PayoutUpiChange, Wallet, WalletTransaction, WithdrawalRequest

# A UPI virtual payment address: handle@provider, e.g. name.surname@okbank.
UPI_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9]+")
UPI_TAKEN_MESSAGE = "This UPI ID is linked to another account."
UPI_CHANGE_INTERVAL = timedelta(days=30)


def normalize_upi(value):
    """" Name@OkBank " -> "name@okbank"."""
    return value.strip().lower() if isinstance(value, str) else ""


def next_upi_change_at(user):
    """When the saved UPI ID may next be changed; None when a change is allowed now."""
    if not user.upi_id or not user.upi_id_changed_at:
        return None
    return user.upi_id_changed_at + UPI_CHANGE_INTERVAL


@transaction.atomic
def set_payout_upi(*, user, upi_id, now=None):
    """Save or change the user's payout UPI ID.

    One account per UPI ID (also a unique column). The first save is always
    allowed; after that a change is allowed once every 30 days. Every save is
    logged in PayoutUpiChange. Returns the updated user.
    """
    now = now or timezone.now()
    upi = normalize_upi(upi_id)
    if not upi:
        raise ValidationError("Enter your UPI ID.")
    if len(upi) > 255 or not UPI_ID_PATTERN.fullmatch(upi):
        raise ValidationError("Enter a valid UPI ID, for example name@bank.")

    User = get_user_model()
    locked = User.objects.select_for_update().get(pk=user.pk)
    if locked.upi_id == upi:
        return locked
    allowed_at = next_upi_change_at(locked)
    if allowed_at and now < allowed_at:
        raise ValidationError(
            f"You can change your UPI ID once every 30 days. The next change is allowed on "
            f"{timezone.localtime(allowed_at):%d %b %Y}."
        )
    if User.objects.filter(upi_id=upi).exclude(pk=locked.pk).exists():
        raise ValidationError(UPI_TAKEN_MESSAGE)
    previous = locked.upi_id
    try:
        with transaction.atomic():
            User.objects.filter(pk=locked.pk).update(upi_id=upi, upi_id_changed_at=now)
    except IntegrityError:
        # Another account saved the same UPI ID at the same moment.
        raise ValidationError(UPI_TAKEN_MESSAGE)
    PayoutUpiChange.objects.create(user=locked, previous_upi_id=previous, new_upi_id=upi)
    locked.upi_id, locked.upi_id_changed_at = upi, now
    return locked


def _locked_wallet(user):
    """The user's wallet, created on first use, locked until the transaction ends."""
    wallet, _ = Wallet.objects.select_for_update().get_or_create(
        user=user,
        defaults={"balance": Decimal("0.00000000")},
    )
    return wallet


@transaction.atomic
def credit_approved_task_reward(*, claim_id):
    """
    Credit the reward for an approved task.
    The same task reward cannot be credited twice.
    """

    try:
        # The claim row lock serializes concurrent credits for this claim; the
        # unique WalletTransaction.reward_claim column is the database backstop.
        claim = TaskClaim.objects.select_related("task").select_for_update().get(pk=claim_id)
    except TaskClaim.DoesNotExist:
        raise ValidationError("Task submission not found.")

    task = claim.task
    if claim.status != TaskClaim.Status.APPROVED:
        raise ValidationError("Only approved tasks can receive rewards.")

    if claim.user_id is None:
        raise ValidationError("This task submission has no user.")

    amount = task.reward_amount

    if amount <= Decimal("0"):
        raise ValidationError("Task reward must be greater than zero.")

    # Serialize wallet operations for this user on databases
    # that support row-level locking.
    user = get_user_model().objects.select_for_update().get(pk=claim.user_id)
    wallet = _locked_wallet(user)

    # reward_claim on every row since wallet.0005; the claim/type match also
    # finds an older reward row the backfill could not link.
    existing = WalletTransaction.objects.filter(
        Q(reward_claim=claim) | Q(claim=claim, transaction_type=WalletTransaction.TransactionType.TASK_REWARD),
    ).first()

    if existing:
        return existing

    new_balance = wallet.balance + amount

    # Create the unique ledger entry before updating the balance.
    # If the unique constraint rejects a duplicate, the balance
    # has not yet been changed.
    try:
        with transaction.atomic():
            ledger_entry = WalletTransaction.objects.create(
                wallet=wallet,
                transaction_type=(WalletTransaction.TransactionType.TASK_REWARD),
                direction=WalletTransaction.Direction.CREDIT,
                amount=amount,
                balance_after=new_balance,
                task=task,
                claim=claim,
                reward_claim=claim,
                description=f"Reward for Task #{task.pk}, claim #{claim.pk}",
            )
    except IntegrityError:
        existing = WalletTransaction.objects.filter(reward_claim=claim).first()

        if existing:
            return existing

        raise

    wallet.balance = new_balance
    wallet.save(update_fields=["balance", "updated_at"])

    return ledger_entry


def credit_referral_bonus(*, referral, description):
    """Credit a referral's bonus to its referrer, once. Run inside the caller's transaction.

    The caller holds the referral row lock; the unique WalletTransaction.referral
    column is the database backstop. Returns the ledger row (the existing one
    when the referral was already credited).
    """
    existing = WalletTransaction.objects.filter(referral=referral).first()
    if existing:
        return existing
    amount = Decimal(referral.bonus_amount_snapshot).quantize(Decimal("0.00000001"))
    if amount <= Decimal("0"):
        raise ValidationError("The referral bonus must be greater than zero.")
    wallet = _locked_wallet(get_user_model().objects.select_for_update().get(pk=referral.referrer_id))
    new_balance = wallet.balance + amount
    with transaction.atomic():
        entry = WalletTransaction.objects.create(
            wallet=wallet,
            transaction_type=WalletTransaction.TransactionType.REFERRAL_BONUS,
            direction=WalletTransaction.Direction.CREDIT,
            amount=amount,
            balance_after=new_balance,
            referral=referral,
            description=description[:255],
        )
    wallet.balance = new_balance
    wallet.save(update_fields=["balance", "updated_at"])
    return entry


def _check_can_withdraw(user):
    User = get_user_model()
    if user.approval_status == User.ApprovalStatus.DISAPPROVED:
        raise ValidationError("Your account was disapproved after reaching the strike limit. Withdrawals are unavailable until an administrator reapproves it.")
    if user.approval_status != User.ApprovalStatus.APPROVED:
        raise ValidationError("Your account is not approved.")


def withdrawal_hold_until(withdrawal, hold_hours=None):
    """When a user's first withdrawal may be approved, or None when it is not held.

    "First" means the user has no approved withdrawal yet. The hold runs from
    the request time, with the hours configured now (Site Configuration).
    """
    if withdrawal.status != WithdrawalRequest.Status.PENDING:
        return None
    if hold_hours is None:
        hold_hours = SiteConfiguration.objects.filter(pk=1).values_list("first_withdrawal_hold_hours", flat=True).first() or 0
    if not hold_hours:
        return None
    if WithdrawalRequest.objects.filter(user_id=withdrawal.user_id, status=WithdrawalRequest.Status.APPROVED).exists():
        return None
    return withdrawal.requested_at + timedelta(hours=hold_hours)


def _check_daily_limit(user, amount, configuration):
    """Pending and approved requests made today (IST) plus ``amount`` must stay within the daily maximum."""
    cap = configuration.withdrawal_max_per_day if configuration else Decimal("0")
    if not cap:
        return
    today = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    used = sum(WithdrawalRequest.objects.filter(user=user, requested_at__gte=today).exclude(
        status=WithdrawalRequest.Status.REJECTED).values_list("amount", flat=True), Decimal("0"))
    if used + amount > cap:
        left = max(cap - used, Decimal("0")).quantize(Decimal("0.01"))
        raise ValidationError(f"You can withdraw up to ₹{cap} per day. ₹{left} is left for today.")


@transaction.atomic
def create_withdrawal_request(*, user, amount, payout_method=WithdrawalRequest.PayoutMethod.UPI):
    """Create a withdrawal request to the user's saved UPI ID and reserve funds."""

    if not user.is_authenticated or not user.is_active:
        raise ValidationError("An active logged-in user is required.")

    if user.is_staff or user.is_superuser:
        raise ValidationError("Administrators cannot request withdrawals.")

    _check_can_withdraw(user)

    if not isinstance(amount, Decimal):
        try:
            amount = Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError):
            raise ValidationError("Enter a valid withdrawal amount.")

    if not amount.is_finite() or amount <= Decimal("0"):
        raise ValidationError("Amount must be greater than zero.")

    configuration = SiteConfiguration.objects.filter(pk=1).first()
    minimum_amount = (
        configuration.min_withdrawal_amount if configuration else Decimal("100.00")
    )

    if amount < minimum_amount:
        raise ValidationError(f"Minimum withdrawal amount is ₹{minimum_amount}.")

    try:
        normalized_amount = amount.quantize(Decimal("0.00000001"))
    except InvalidOperation:
        raise ValidationError("Amount exceeds supported precision.")

    if normalized_amount != amount or amount.adjusted() >= 12:
        raise ValidationError(
            "Amount supports at most 12 integer digits and 8 decimal places."
        )

    if payout_method != WithdrawalRequest.PayoutMethod.UPI:
        raise ValidationError("New withdrawals can only be sent to a UPI ID.")

    locked_user = get_user_model().objects.select_for_update().get(pk=user.pk)
    # Re-check on the locked row: a strike-limit disapproval may have landed
    # after this request loaded the user.
    _check_can_withdraw(locked_user)
    # Always the saved UPI ID, never one sent with the request.
    payout_details = locked_user.upi_id
    if not payout_details:
        raise ValidationError("Add your UPI ID before requesting a withdrawal.")

    # After the user row lock, so two requests at once cannot both pass the limit.
    _check_daily_limit(locked_user, normalized_amount, configuration)

    wallet = _locked_wallet(locked_user)

    if wallet.balance < normalized_amount:
        raise ValidationError("Insufficient wallet balance.")

    new_balance = wallet.balance - normalized_amount

    withdrawal = WithdrawalRequest.objects.create(
        user=locked_user,
        amount=normalized_amount,
        payout_method=payout_method,
        payout_details=payout_details,
    )

    WalletTransaction.objects.create(
        wallet=wallet,
        transaction_type=WalletTransaction.TransactionType.PAYOUT,
        direction=WalletTransaction.Direction.DEBIT,
        amount=normalized_amount,
        balance_after=new_balance,
        description=f"Withdrawal request #{withdrawal.pk}",
    )

    wallet.balance = new_balance
    wallet.save(update_fields=["balance", "updated_at"])

    # User ID and amount only; never the UPI ID.
    send_admin_alert("withdrawal_request", f"New withdrawal request #{withdrawal.pk} from user #{locked_user.pk}: {rupees(normalized_amount)}.")
    return withdrawal


@transaction.atomic
def review_withdrawal_request(*, withdrawal_id, reviewer, action, review_note=""):
    """Approve a withdrawal or refund it when rejected."""

    if not reviewer.is_authenticated or not reviewer.is_active or not reviewer.is_staff:
        raise ValidationError("Only an active admin can review withdrawals.")

    if action not in ("approve", "reject"):
        raise ValidationError("Action must be approve or reject.")

    if not isinstance(review_note, str) or len(review_note) > 2000:
        raise ValidationError("Review note cannot exceed 2000 characters.")

    try:
        withdrawal = WithdrawalRequest.objects.select_for_update().get(pk=withdrawal_id)
    except WithdrawalRequest.DoesNotExist:
        raise ValidationError("Withdrawal request not found.")

    if withdrawal.status != WithdrawalRequest.Status.PENDING:
        raise ValidationError("This withdrawal has already been reviewed.")

    withdrawal.reviewed_by = reviewer
    withdrawal.review_note = review_note.strip()
    withdrawal.reviewed_at = timezone.now()

    if action == "approve":
        hold_until = withdrawal_hold_until(withdrawal)
        if hold_until and timezone.now() < hold_until:
            raise ValidationError(
                f"This is the user's first withdrawal. It can be approved from "
                f"{timezone.localtime(hold_until):%d %b %Y %H:%M} IST (first-withdrawal hold). You can reject it at any time."
            )
        withdrawal.status = WithdrawalRequest.Status.APPROVED
        # Funds were already deducted when the request was created.
    else:
        wallet = Wallet.objects.select_for_update().get(user_id=withdrawal.user_id)
        new_balance = wallet.balance + withdrawal.amount

        WalletTransaction.objects.create(
            wallet=wallet,
            transaction_type=WalletTransaction.TransactionType.PAYOUT,
            direction=WalletTransaction.Direction.CREDIT,
            amount=withdrawal.amount,
            balance_after=new_balance,
            description=f"Refund for rejected withdrawal #{withdrawal.pk}",
        )

        wallet.balance = new_balance
        wallet.save(update_fields=["balance", "updated_at"])

        withdrawal.status = WithdrawalRequest.Status.REJECTED

    withdrawal.save(
        update_fields=[
            "status",
            "reviewed_by",
            "review_note",
            "reviewed_at",
            "updated_at",
        ]
    )

    return withdrawal
