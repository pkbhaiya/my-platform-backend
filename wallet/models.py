from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class Wallet(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="wallet",
    )

    # Keep precise decimal values; never use float for money.
    balance = models.DecimalField(
        max_digits=20,
        decimal_places=8,
        default=Decimal("0.00000000"),
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(balance__gte=0),
                name="wallet_balance_nonnegative",
            ),
        ]

    def __str__(self):
        return f"Wallet - {self.user}"


class WalletTransaction(models.Model):

    class TransactionType(models.TextChoices):
        TASK_REWARD = "task_reward", "Task Reward"
        PAYOUT = "payout", "Payout"
        ADJUSTMENT = "adjustment", "Admin Adjustment"
        REFERRAL_BONUS = "referral_bonus", "Referral Bonus"

    class Direction(models.TextChoices):
        CREDIT = "credit", "Credit"
        DEBIT = "debit", "Debit"

    wallet = models.ForeignKey(
        Wallet,
        on_delete=models.PROTECT,
        related_name="transactions",
    )

    transaction_type = models.CharField(
        max_length=20,
        choices=TransactionType.choices,
    )

    direction = models.CharField(
        max_length=6,
        choices=Direction.choices,
    )

    # Always a positive amount; direction determines credit or debit.
    amount = models.DecimalField(
        max_digits=20,
        decimal_places=8,
    )

    # Balance immediately after this transaction.
    balance_after = models.DecimalField(
        max_digits=20,
        decimal_places=8,
    )

    # Used to prevent the same task reward from being credited twice.
    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wallet_transactions",
    )
    claim = models.ForeignKey(
        "tasks.TaskClaim",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="wallet_transactions",
    )
    # The claim again, but only on its task_reward row and unique: one reward per
    # claim on every database (NULLs never collide in a unique index).
    reward_claim = models.OneToOneField(
        "tasks.TaskClaim",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="reward_entry",
    )

    # The referral a referral_bonus credit pays; unique, so a referral is paid at most once.
    referral = models.OneToOneField(
        "referrals.Referral",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="bonus_entry",
    )

    description = models.CharField(
        max_length=255,
        blank=True,
        default="",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="wallet_tx_amount_positive",
            ),
            models.CheckConstraint(
                condition=Q(balance_after__gte=0),
                name="wallet_tx_balance_nonnegative",
            ),
            models.CheckConstraint(
                condition=(~Q(transaction_type="task_reward") | Q(task__isnull=False)),
                name="task_reward_requires_task",
            ),
            # claim__isnull=False is explicit: with claim NULL the equality is
            # unknown, and a CHECK that evaluates to unknown passes.
            models.CheckConstraint(
                condition=Q(reward_claim__isnull=True) | (
                    Q(transaction_type="task_reward") & Q(claim__isnull=False) & Q(reward_claim=models.F("claim"))
                ),
                name="wallet_tx_reward_claim_matches_claim",
            ),
            # A referral bonus always names its referral, and only a referral bonus does.
            models.CheckConstraint(
                condition=(Q(transaction_type="referral_bonus") & Q(referral__isnull=False))
                | (~Q(transaction_type="referral_bonus") & Q(referral__isnull=True)),
                name="wallet_tx_referral_bonus_link",
            ),
        ]
        indexes = [
            models.Index(
                fields=["wallet", "created_at"],
                name="wallet_tx_history_idx",
            ),
        ]

    def __str__(self):
        return (
            f"{self.get_direction_display()} "
            f"₹{self.amount} - Wallet #{self.wallet_id}"
        )


class WithdrawalRequest(models.Model):

    class PayoutMethod(models.TextChoices):
        UPI = "upi", "UPI"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="withdrawal_requests",
    )

    amount = models.DecimalField(
        max_digits=20,
        decimal_places=8,
        validators=[MinValueValidator(Decimal("0.00000001"))],
    )

    payout_method = models.CharField(
        max_length=10,
        choices=PayoutMethod.choices,
    )

    payout_details = models.CharField(max_length=500)

    status = models.CharField(
        max_length=10,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_withdrawals",
    )

    review_note = models.TextField(blank=True, default="")

    requested_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="withdrawal_amount_positive",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "requested_at"],
                name="withdraw_status_date_idx",
            ),
            models.Index(
                fields=["user", "status"],
                name="withdraw_user_status_idx",
            ),
        ]

    def __str__(self):
        return f"Withdrawal #{self.pk} - {self.status}"


class PayoutUpiChange(models.Model):
    """Every time a user saves or changes their payout UPI ID (audit log)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payout_upi_changes")
    previous_upi_id = models.CharField(max_length=255, null=True, blank=True)
    new_upi_id = models.CharField(max_length=255)
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-changed_at", "-id"]
        indexes = [models.Index(fields=["user", "changed_at"], name="payout_upi_change_user_idx")]

    def __str__(self):
        return f"UPI change for user #{self.user_id}"


class ReconciliationRun(models.Model):
    """One run of reconcile_wallets: what was checked and every mismatch found.

    Balances are never corrected automatically. A run with mismatches stays
    flagged on the Admin Dashboard until an admin marks it reviewed.
    """

    started_at = models.DateTimeField()
    finished_at = models.DateTimeField()
    wallets_checked = models.PositiveIntegerField()
    mismatches_count = models.PositiveIntegerField()
    details = models.JSONField(default=dict)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    review_note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-started_at", "-id"]

    def __str__(self):
        return f"Reconciliation #{self.pk}: {self.mismatches_count} mismatch(es)"
