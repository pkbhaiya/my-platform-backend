"""Admin wallet balances, checked by the daily reconcile_wallets run. Read-only: it reports, never corrects.

For every wallet: balance == funds added + refunds + credit adjustments -
order charges - debit adjustments (exact Decimal), and equals the latest
balance_after. For every order charged to a wallet: exactly one order_charge
row of that wallet for exactly charge_amount, which is the order value
ROUND_HALF_UP. Every Partial or Cancelled charged order has exactly one refund
of the amount it is owed (none when that rounds to ₹0.00); no other order has
one. API orders placed before prepaid balances (no charge, created before
their wallet) are listed as one notice per wallet, as are staff users without
a wallet and wallets whose owner is no longer staff.
"""

from collections import defaultdict
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db.models import Q

from .admin_wallets import charge_for, refund_due
from .models import AdminWallet, AdminWalletTransaction, Order

Type = AdminWalletTransaction.Type
Direction = AdminWalletTransaction.Direction
# The direction each type must have; adjustments may go either way.
REQUIRED_DIRECTION = {Type.ADD_FUNDS: Direction.CREDIT, Type.REFUND: Direction.CREDIT, Type.ORDER_CHARGE: Direction.DEBIT}


def text(value):
    return None if value is None else format(value, "f")


def reconcile_admin_wallets():
    """Returns (wallets checked, mismatches, notices) in reconcile_wallets' format."""
    from common.analytics import orders_with_delivery

    mismatches, notices = [], []
    ledger = defaultdict(list)
    for row in AdminWalletTransaction.objects.order_by("created_at", "id").values(
        "id", "wallet_id", "transaction_type", "direction", "amount", "balance_after", "order_id",
    ):
        ledger[row["wallet_id"]].append(row)

    wallets = {wallet["id"]: wallet for wallet in AdminWallet.objects.values("id", "admin_id", "admin__is_staff", "balance", "created_at")}
    for wallet_id, wallet in wallets.items():
        rows = ledger.get(wallet_id, [])
        totals = defaultdict(lambda: Decimal("0"))
        for row in rows:
            required = REQUIRED_DIRECTION.get(row["transaction_type"])
            if row["transaction_type"] not in Type.values or (required and row["direction"] != required) or row["amount"] <= 0:
                mismatches.append({"check": "api_ledger_row_invalid", "admin_wallet_id": wallet_id, "transaction_id": row["id"],
                                   "transaction_type": row["transaction_type"], "direction": row["direction"], "amount": text(row["amount"]),
                                   "message": "An admin wallet ledger row has an impossible type, direction or amount."})
            totals[(row["transaction_type"], row["direction"])] += row["amount"]
        expected = (
            totals[(Type.ADD_FUNDS, Direction.CREDIT)] + totals[(Type.REFUND, Direction.CREDIT)]
            + totals[(Type.ADJUSTMENT, Direction.CREDIT)] - totals[(Type.ORDER_CHARGE, Direction.DEBIT)]
            - totals[(Type.ADJUSTMENT, Direction.DEBIT)]
        )
        latest = rows[-1]["balance_after"] if rows else Decimal("0")
        if expected != wallet["balance"] or latest != wallet["balance"]:
            mismatches.append({"check": "admin_wallet_balance", "admin_wallet_id": wallet_id, "admin_id": wallet["admin_id"],
                               "message": "An admin wallet's balance does not match its ledger.",
                               "balance": text(wallet["balance"]), "ledger_balance": text(expected), "latest_balance_after": text(latest)})
        if not wallet["admin__is_staff"]:
            notices.append({"check": "admin_wallet_owner_not_staff", "admin_wallet_id": wallet_id, "admin_id": wallet["admin_id"],
                            "balance": text(wallet["balance"]),
                            "message": "This wallet's owner is no longer staff: its key is refused; the balance is kept."})

    without = list(get_user_model().objects.filter(is_staff=True, admin_wallet__isnull=True).values_list("pk", flat=True))
    if without:
        notices.append({"check": "staff_without_admin_wallet", "admin_ids": without,
                        "message": "Staff users without an admin wallet (one is created when they open My Wallet)."})

    charges, refunds = defaultdict(list), defaultdict(list)
    for rows in ledger.values():
        for row in rows:
            if row["transaction_type"] == Type.ORDER_CHARGE:
                charges[row["order_id"]].append(row)
            elif row["transaction_type"] == Type.REFUND:
                refunds[row["order_id"]].append(row)
    for kind, bucket in (("charge", charges), ("refund", refunds)):
        if None in bucket:
            for row in bucket.pop(None):
                mismatches.append({"check": f"api_{kind}_without_order", "admin_wallet_id": row["wallet_id"], "transaction_id": row["id"],
                                   "message": f"An admin wallet {kind} does not name its order."})

    legacy = defaultdict(int)
    linked = set(charges) | set(refunds)
    orders = orders_with_delivery().filter(Q(admin_wallet__isnull=False) | Q(pk__in=linked)).order_by("id")
    for order in orders:
        own_charges, own_refunds = charges.get(order.pk, []), refunds.get(order.pk, [])
        entries = own_charges + own_refunds
        if order.admin_wallet_id is None or any(row["wallet_id"] != order.admin_wallet_id for row in entries):
            mismatches.append({"check": "api_entry_for_other_order", "order_id": order.pk, "admin_wallet_id": order.admin_wallet_id,
                               "transaction_ids": [row["id"] for row in entries],
                               "message": "An order has charge or refund entries on a wallet that did not place it."})
            continue
        if order.charge_amount is None:
            if entries:
                mismatches.append({"check": "api_entry_for_uncharged_order", "order_id": order.pk, "admin_wallet_id": order.admin_wallet_id,
                                   "transaction_ids": [row["id"] for row in entries],
                                   "message": "An order without a recorded charge has charge or refund entries."})
            elif order.created_at >= wallets[order.admin_wallet_id]["created_at"]:
                mismatches.append({"check": "api_order_without_charge", "order_id": order.pk, "admin_wallet_id": order.admin_wallet_id,
                                   "message": "An API order placed after its wallet existed has no recorded charge."})
            else:
                legacy[order.admin_wallet_id] += 1
            continue
        if order.charge_amount != charge_for(order.total_order_value):
            mismatches.append({"check": "api_charge_amount", "order_id": order.pk, "admin_wallet_id": order.admin_wallet_id,
                               "charge_amount": text(order.charge_amount), "expected": text(charge_for(order.total_order_value)),
                               "message": "An order's charge does not match its value (rate x quantity / 1,000, rounded half up)."})
        if len(own_charges) != 1 or own_charges[0]["amount"] != order.charge_amount:
            mismatches.append({"check": "api_order_charge", "order_id": order.pk, "admin_wallet_id": order.admin_wallet_id,
                               "found": len(own_charges), "amounts": [text(row["amount"]) for row in own_charges],
                               "charge_amount": text(order.charge_amount),
                               "message": "A charged API order does not have exactly one matching charge entry."})
        due = refund_due(order, order.delivered)
        if (due > 0 and (len(own_refunds) != 1 or own_refunds[0]["amount"] != due)) or (due == 0 and own_refunds):
            mismatches.append({"check": "api_order_refund", "order_id": order.pk, "admin_wallet_id": order.admin_wallet_id,
                               "status": order.fulfillment_status, "expected": text(due), "found": len(own_refunds),
                               "amounts": [text(row["amount"]) for row in own_refunds],
                               "message": "An API order does not have exactly the refund it is owed."})
    for wallet_id, count in sorted(legacy.items()):
        notices.append({"check": "api_orders_before_balances", "admin_wallet_id": wallet_id, "orders": count,
                        "message": "API orders placed before prepaid balances: never charged, so never refunded."})
    return len(wallets), mismatches, notices
