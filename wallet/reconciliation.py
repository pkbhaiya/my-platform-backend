"""Daily wallet reconciliation. Read-only: it reports, it never corrects.

Every wallet's balance is recomputed from its ledger (credits - debits, exact
Decimal) and compared with Wallet.balance and with the latest balance_after.
The ledger is also checked against claims and withdrawals: every task_reward
credit belongs to an approved claim and no claim is credited twice; every
withdrawal has its payout debit ("Withdrawal request #N", written by
create_withdrawal_request); every rejected withdrawal, and only those, has its
refund credit ("Refund for rejected withdrawal #N").

The same run checks admin wallet balances, charges and refunds
(orders.reconciliation).

Mismatches are counted and alerted. Notices (e.g. a legacy reward recorded
without its claim) are listed but cannot be verified, so they do not alert.
"""

import re
from collections import defaultdict
from decimal import Decimal

from django.utils import timezone

from common.notifications import send_admin_alert
from tasks.models import TaskClaim

from .models import ReconciliationRun, Wallet, WalletTransaction, WithdrawalRequest

DEBIT_DESCRIPTION = re.compile(r"Withdrawal request #(\d+)")
REFUND_DESCRIPTION = re.compile(r"Refund for rejected withdrawal #(\d+)")


def text(value):
    return format(value, "f")


def reconcile_wallets():
    """Check every wallet and the ledger, store a ReconciliationRun and alert on mismatches."""
    started = timezone.now()
    mismatches, notices = [], []

    ledger = defaultdict(list)
    for row in WalletTransaction.objects.order_by("created_at", "id").values(
        "id", "wallet_id", "transaction_type", "direction", "amount", "balance_after", "claim_id", "description",
    ):
        ledger[row["wallet_id"]].append(row)

    wallets = list(Wallet.objects.order_by("id").values("id", "user_id", "balance"))
    for wallet in wallets:
        rows = ledger.get(wallet["id"], [])
        credits = sum((row["amount"] for row in rows if row["direction"] == WalletTransaction.Direction.CREDIT), Decimal("0"))
        debits = sum((row["amount"] for row in rows if row["direction"] == WalletTransaction.Direction.DEBIT), Decimal("0"))
        expected = credits - debits
        latest = rows[-1]["balance_after"] if rows else Decimal("0")
        if expected != wallet["balance"] or latest != wallet["balance"]:
            mismatches.append({
                "check": "wallet_balance",
                "wallet_id": wallet["id"],
                "user_id": wallet["user_id"],
                "message": "Wallet balance does not match its ledger.",
                "wallet_balance": text(wallet["balance"]),
                "ledger_balance": text(expected),
                "latest_balance_after": text(latest),
            })

    rewards = [row for rows in ledger.values() for row in rows if row["transaction_type"] == WalletTransaction.TransactionType.TASK_REWARD]
    claim_status = dict(TaskClaim.objects.filter(pk__in={row["claim_id"] for row in rewards if row["claim_id"]}).values_list("pk", "status"))
    per_claim = defaultdict(list)
    for row in rewards:
        if row["claim_id"] is None:
            notices.append({"check": "reward_without_claim", "transaction_id": row["id"], "wallet_id": row["wallet_id"],
                            "message": "A task reward has no claim recorded, so its approval cannot be verified."})
            continue
        per_claim[row["claim_id"]].append(row["id"])
        if claim_status.get(row["claim_id"]) != TaskClaim.Status.APPROVED:
            mismatches.append({"check": "reward_on_unapproved_claim", "transaction_id": row["id"], "claim_id": row["claim_id"],
                               "claim_status": claim_status.get(row["claim_id"]),
                               "message": "A task reward was credited for a claim that is not approved."})
    for claim_id, transaction_ids in per_claim.items():
        if len(transaction_ids) > 1:
            mismatches.append({"check": "claim_credited_twice", "claim_id": claim_id, "transaction_ids": transaction_ids,
                               "message": "One claim has more than one task reward credit."})

    # Referral bonuses: a credit only for a paid referral, and every paid referral credited.
    from referrals.models import Referral

    bonus_rows = [row for rows in ledger.values() for row in rows if row["transaction_type"] == WalletTransaction.TransactionType.REFERRAL_BONUS]
    credited = set(WalletTransaction.objects.filter(pk__in=[row["id"] for row in bonus_rows]).values_list("referral_id", flat=True))
    referral_status = dict(Referral.objects.filter(pk__in=credited).values_list("pk", "status"))
    for referral_id in credited:
        if referral_status.get(referral_id) != Referral.Status.PAID:
            mismatches.append({"check": "bonus_on_unpaid_referral", "referral_id": referral_id, "status": referral_status.get(referral_id),
                               "message": "A referral bonus was credited for a referral that is not paid."})
    for referral_id in Referral.objects.filter(status=Referral.Status.PAID).exclude(pk__in=credited).values_list("pk", flat=True):
        mismatches.append({"check": "paid_referral_without_credit", "referral_id": referral_id,
                           "message": "A referral is marked paid but has no bonus credit."})

    debits, refunds = defaultdict(list), defaultdict(list)
    for rows in ledger.values():
        for row in rows:
            if row["transaction_type"] != WalletTransaction.TransactionType.PAYOUT:
                continue
            pattern, bucket = ((DEBIT_DESCRIPTION, debits) if row["direction"] == WalletTransaction.Direction.DEBIT
                               else (REFUND_DESCRIPTION, refunds))
            match = pattern.fullmatch(row["description"] or "")
            if match:
                bucket[int(match.group(1))].append(row)
            else:
                notices.append({"check": "unlinked_payout_row", "transaction_id": row["id"], "wallet_id": row["wallet_id"],
                                "message": "A payout ledger row does not name its withdrawal."})
    withdrawals = WithdrawalRequest.objects.select_related("user__wallet").order_by("id")
    for withdrawal in withdrawals:
        wallet_id = getattr(getattr(withdrawal.user, "wallet", None), "pk", None)
        own = lambda rows: [row for row in rows if row["wallet_id"] == wallet_id and row["amount"] == withdrawal.amount]  # noqa: E731
        if len(own(debits.get(withdrawal.pk, []))) != 1:
            mismatches.append({"check": "withdrawal_debit", "withdrawal_id": withdrawal.pk, "user_id": withdrawal.user_id,
                               "found": len(debits.get(withdrawal.pk, [])),
                               "message": "A withdrawal does not have exactly one matching payout debit."})
        refunded = len(own(refunds.get(withdrawal.pk, [])))
        if withdrawal.status == WithdrawalRequest.Status.REJECTED and refunded != 1:
            mismatches.append({"check": "missing_refund", "withdrawal_id": withdrawal.pk, "user_id": withdrawal.user_id, "found": refunded,
                               "message": "A rejected withdrawal does not have exactly one matching refund credit."})
        elif withdrawal.status != WithdrawalRequest.Status.REJECTED and refunds.get(withdrawal.pk):
            mismatches.append({"check": "unexpected_refund", "withdrawal_id": withdrawal.pk, "status": withdrawal.status,
                               "message": "A refund was credited for a withdrawal that is not rejected."})

    from orders.reconciliation import reconcile_admin_wallets

    admin_wallets_checked, api_mismatches, api_notices = reconcile_admin_wallets()
    mismatches += api_mismatches
    notices += api_notices

    run = ReconciliationRun.objects.create(
        started_at=started,
        finished_at=timezone.now(),
        wallets_checked=len(wallets),
        mismatches_count=len(mismatches),
        details={"mismatches": mismatches, "notices": notices, "admin_wallets_checked": admin_wallets_checked},
    )
    if mismatches:
        send_admin_alert("reconciliation_mismatch", (
            f"Wallet reconciliation run #{run.pk} found {len(mismatches)} mismatch(es). "
            f"Nothing was corrected. Review it in Admin → Reconciliation."
        ))
    return run


def unreviewed_reconciliation_alert():
    """The Dashboard banner: runs with mismatches that no admin has reviewed yet (None when clear)."""
    pending = ReconciliationRun.objects.filter(mismatches_count__gt=0, reviewed_at__isnull=True)
    latest = pending.order_by("-started_at", "-id").first()
    if latest is None:
        return None
    return {"runs": pending.count(), "latest_run_id": latest.pk, "latest_mismatches": latest.mismatches_count,
            "latest_started_at": latest.started_at.isoformat()}
