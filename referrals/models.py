from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import F, Q


class Referral(models.Model):
    """One referred user, created only at signup and never changed to another referrer.

    The referrer earns one bonus when the referred user reaches the required
    number of approved claims within the window. The terms (required tasks,
    bonus, deadline) are copied from Site Configuration at signup.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        QUALIFIED = "qualified", "Qualified (unpaid)"
        PAID = "paid", "Paid"
        FLAGGED = "flagged", "Flagged for review"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"

    referrer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="referrals_made")
    # One referral per user, ever.
    referred = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="referral")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, db_index=True)
    required_tasks_snapshot = models.PositiveIntegerField()
    bonus_amount_snapshot = models.DecimalField(max_digits=12, decimal_places=2)
    # Copied from referral_qualify_within_days at signup; None means no limit.
    qualify_deadline = models.DateTimeField(null=True, blank=True)
    # Set when an admin approves a flagged referral: shared signals no longer flag it.
    abuse_cleared = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    qualified_at = models.DateTimeField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    flag_reason = models.CharField(max_length=255, blank=True, default="")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=~Q(referrer=F("referred")), name="referral_not_self"),
            models.CheckConstraint(condition=Q(bonus_amount_snapshot__gt=Decimal("0")), name="referral_bonus_positive"),
            models.CheckConstraint(condition=Q(required_tasks_snapshot__gte=1), name="referral_required_tasks_positive"),
        ]
        indexes = [
            models.Index(fields=["referrer", "status"], name="referral_referrer_status_idx"),
            models.Index(fields=["status", "qualify_deadline"], name="referral_status_deadline_idx"),
            models.Index(fields=["paid_at"], name="referral_paid_at_idx"),
        ]

    def __str__(self):
        return f"Referral #{self.pk}: user #{self.referrer_id} -> user #{self.referred_id} ({self.status})"
