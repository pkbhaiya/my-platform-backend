from datetime import timedelta

from django.conf import settings
from django.db import models
from django.db.models import Q

from services_catalog.models import Service


class Task(models.Model):

    class Status(models.TextChoices):
        AVAILABLE = "available", "Available"
        # Every slot is held, but some held claims are not approved yet.
        FULLY_CLAIMED = "fully_claimed", "Fully Claimed"
        # Hidden and unclaimable after Target Not Available reports, until an admin resumes it.
        PAUSED = "paused", "Paused"
        # Every slot holds an approved claim.
        COMPLETED = "completed", "Completed"
        # Closed by an admin; approved work stays credited, open claims were cancelled.
        CANCELLED = "cancelled", "Cancelled"
        UNAVAILABLE = "unavailable", "Unavailable"
        # Per-unit statuses from before aggregate Tasks; no longer set.
        CLAIMED = "claimed", "Claimed"
        WORK_SUBMITTED = "work_submitted", "Work Submitted"
        POST_NOT_AVAILABLE = "post_not_available", "Target Not Available"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"

    # Order-backed work keeps its order relationship; direct admin tasks may stand alone.
    order = models.ForeignKey(
        "orders.Order",
        on_delete=models.PROTECT,
        related_name="tasks",
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=150, blank=True, default="")
    unit_number = models.PositiveIntegerField(default=1)
    # One Task may provide many independently consumed claim slots.
    total_slots = models.PositiveIntegerField(default=1, editable=False)
    remaining_slots = models.PositiveIntegerField(default=1, editable=False)
    admin_request_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    admin_request_quantity = models.PositiveIntegerField(default=1, editable=False)

    # Snapshot details: these should not change if service pricing changes later
    target_link = models.URLField(max_length=2048)
    price_per_1000_snapshot = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        null=True,
        blank=True,
    )
    user_reward_percentage_snapshot = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        null=True,
        blank=True,
    )
    reward_amount = models.DecimalField(
        max_digits=18,
        decimal_places=8,
        default=0,
    )

    # Assignment
    assigned_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_tasks",
    )

    status = models.CharField(
        max_length=24,
        choices=Status.choices,
        default=Status.AVAILABLE,
        db_index=True,
    )

    # Submission and admin review
    submission_note = models.TextField(blank=True, default="")
    review_note = models.TextField(blank=True, default="")

    claimed_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_tasks",
    )

    class CancelReason(models.TextChoices):
        TARGET_REMOVED = "target_removed", "Target removed"
        TARGET_PRIVATE = "target_private", "Target is private"
        BROKEN_LINK = "broken_link", "Broken link"
        OTHER = "other", "Other"
        # Set only by the order-expiry sweep, never chosen by an admin.
        EXPIRED = "expired", "Order expired"

    # Set together, once, when an admin cancels the Task.
    cancel_reason = models.CharField(max_length=20, choices=CancelReason.choices, blank=True, default="")
    cancel_note = models.TextField(blank=True, default="")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="cancelled_tasks",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            # No condition needed: standalone Tasks have a NULL order, and NULLs
            # never collide in a unique index. MySQL ignores conditional
            # constraints, so this form keeps the guarantee there too.
            models.UniqueConstraint(fields=["order"], name="uniq_aggregate_task_per_order"),
            models.CheckConstraint(condition=Q(total_slots__gte=1), name="task_total_slots_positive"),
            models.CheckConstraint(condition=Q(remaining_slots__gte=0), name="task_remaining_nonnegative"),
            models.CheckConstraint(condition=Q(remaining_slots__lte=models.F("total_slots")), name="task_remaining_lte_capacity"),
        ]
        indexes = [
            models.Index(
                fields=["order", "status"],
                name="task_order_status_idx",
            ),
            models.Index(
                fields=["assigned_user", "status"],
                name="task_user_status_idx",
            ),
        ]

    def __str__(self):
        return f"Task #{self.pk} - {self.get_status_display()}"

    @property
    def display_name(self):
        """The admin title, else the Order's service name, else ``Task #id``.

        Select ``order__service`` with the Task to avoid a query per row.
        """
        if self.title:
            return self.title
        if self.order_id:
            return self.order.service.name
        return f"Task #{self.pk}"

    @property
    def target_type(self):
        """The Order's snapshotted target type; a standalone Task has none, so "other"."""
        return self.order.target_type_snapshot if self.order_id else Service.TargetType.OTHER


class TaskClaim(models.Model):
    class Status(models.TextChoices):
        CLAIMED = "claimed", "Claimed"
        WORK_SUBMITTED = "work_submitted", "Work Submitted"
        # Stored value kept from before the rename; every label says "Target Not Available".
        POST_NOT_AVAILABLE = "post_not_available", "Target Not Available"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        # Still `claimed` when the claim timeout ran out; the slot was released.
        EXPIRED = "expired", "Expired"
        # Claimed or awaiting review when an admin cancelled the Task: no reward, no strike.
        CANCELLED = "cancelled", "Cancelled"
        UNAVAILABLE = "unavailable", "Unavailable"

    class Result(models.TextChoices):
        WORK_DONE = "work_done", "Work Done"
        POST_NOT_AVAILABLE = "post_not_available", "Target Not Available"

    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="claims")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_claims")
    # The user while this claim is `claimed`, otherwise NULL; unique, so a user
    # holds at most one active claim. A plain unique column works on every
    # database (NULLs never collide), unlike a conditional unique constraint.
    active_for_user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        editable=False,
        related_name="active_task_claim",
    )
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.CLAIMED, db_index=True)
    submitted_result = models.CharField(max_length=24, choices=Result.choices, null=True, blank=True)
    submission_note = models.TextField(blank=True, default="")
    review_note = models.TextField(blank=True, default="")
    claimed_at = models.DateTimeField(auto_now_add=True)
    # Added to the claim deadline: time the claim spent paused by maintenance mode.
    deadline_extension = models.DurationField(default=timedelta(0), editable=False)
    target_opened_at = models.DateTimeField(null=True, blank=True)
    # Wait (seconds) recorded with the first open; null only on claims opened
    # before the wait became configurable.
    target_wait_seconds = models.PositiveIntegerField(null=True, blank=True, editable=False)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="reviewed_task_claims")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        constraints = [
            # active_for_user is set exactly while the claim is `claimed`, to its own user.
            models.CheckConstraint(
                condition=Q(active_for_user__isnull=True) | (Q(status="claimed") & Q(active_for_user=models.F("user"))),
                name="taskclaim_active_user_matches_status",
            ),
        ]
        indexes = [models.Index(fields=["task", "status"], name="task_claim_status_idx"), models.Index(fields=["user", "status"], name="user_claim_status_idx")]

    def __str__(self):
        return f"Claim #{self.pk} for Task #{self.task_id}"


# A claim in one of these statuses holds one of its Task's slots; every other
# outcome has released it, so remaining_slots = total_slots - holding claims.
SLOT_HOLDING_STATUSES = (
    TaskClaim.Status.CLAIMED,
    TaskClaim.Status.WORK_SUBMITTED,
    TaskClaim.Status.APPROVED,
)


class TaskUserClaim(models.Model):
    """Permanent uniqueness guard for one user's claim history per aggregate Task."""

    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="claiming_users")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_claim_slots")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["task", "user"], name="uniq_user_task_claim_identity")]
        indexes = [models.Index(fields=["user", "created_at"], name="task_user_claim_identity_idx")]

    def __str__(self):
        return f"User #{self.user_id} has claimed Task #{self.task_id}"


class TaskStrike(models.Model):
    task = models.ForeignKey(
        "tasks.Task",
        on_delete=models.PROTECT,
        related_name="strikes",
    )
    claim = models.OneToOneField("tasks.TaskClaim", on_delete=models.PROTECT, null=True, blank=True, related_name="strike")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="task_strikes",
    )

    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="issued_task_strikes",
    )

    reason = models.CharField(max_length=500)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Strike for Task #{self.task_id}"
