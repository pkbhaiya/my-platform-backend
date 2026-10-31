import math
import time
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from accounts.models import User, UserLevel
from accounts.strike_policy import enforce_strike_limit
from common.models import SiteConfiguration
from common.notifications import send_admin_alert
from referrals.services import on_claim_approved
from orders.admin_wallets import refund_api_order
from orders.duplicates import refuse_if_worked_on_link
from orders.links import normalize_target_url
from orders.models import Order
from services_catalog.models import Service
from tasks.calculations import calculate_task_reward
from wallet.services import credit_approved_task_reward

from .models import SLOT_HOLDING_STATUSES, Task, TaskClaim, TaskStrike, TaskUserClaim

CLAIM_EXPIRED_MESSAGE = "This claim expired because the claim time limit passed, and its slot was released."

# Final states: nothing derived from claims may change them any more.
FINAL_TASK_STATUSES = (Task.Status.CANCELLED, Task.Status.UNAVAILABLE)
FINAL_ORDER_STATUSES = (
    Order.FulfillmentStatus.PARTIAL, Order.FulfillmentStatus.CANCELLED, Order.FulfillmentStatus.REJECTED,
)
# Claims still open when a Task is cancelled; they become `cancelled`.
OPEN_CLAIM_STATUSES = (TaskClaim.Status.CLAIMED, TaskClaim.Status.WORK_SUBMITTED)
# Reasons an admin may choose; `expired` is set only by the order-expiry sweep.
ADMIN_CANCEL_REASONS = tuple(reason for reason in Task.CancelReason.values if reason != Task.CancelReason.EXPIRED)
# Orders the expiry sweep may cancel: not yet completed, partial, cancelled or rejected.
EXPIRABLE_ORDER_STATUSES = (
    Order.FulfillmentStatus.ACCEPTED, Order.FulfillmentStatus.IN_PROGRESS, Order.FulfillmentStatus.UNDER_REVIEW,
)


def get_accessible_level_codes(user):
    approved_count = TaskClaim.objects.filter(user=user, status=TaskClaim.Status.APPROVED).count()
    return set(UserLevel.objects.filter(is_active=True, minimum_approved_tasks__lte=approved_count).values_list("code", flat=True))


def user_can_access_task(user, task):
    if not task.order_id:
        return True
    required_level = task.order.service.eligible_level
    return required_level == Service.EligibleLevel.ALL or required_level in get_accessible_level_codes(user)


@transaction.atomic
def create_tasks_for_order(order_id):
    """Create exactly one aggregate-capacity Task for an accepted order."""

    order = Order.objects.select_for_update().get(pk=order_id)

    existing_tasks = list(Task.objects.filter(order=order).order_by("id"))

    # A retry must not create duplicate tasks or capacity.
    if existing_tasks:
        if len(existing_tasks) == 1 and existing_tasks[0].total_slots == order.quantity:
            return 1
        raise ValidationError("This order has inconsistent task capacity. Please investigate before retrying.")

    if order.fulfillment_status != Order.FulfillmentStatus.ACCEPTED:
        raise ValidationError(
            "Tasks can only be created for an accepted order."
        )

    price = order.price_per_1000_snapshot
    reward_percentage = order.user_reward_percentage_snapshot

    reward = calculate_task_reward(price, reward_percentage)
    Task.objects.create(order=order, unit_number=1, total_slots=order.quantity,
        remaining_slots=order.quantity, target_link=order.target_link,
        price_per_1000_snapshot=price,
        user_reward_percentage_snapshot=reward_percentage, reward_amount=reward)
    return 1


def release_slot(task_id, now):
    """Give one slot back to the Task.

    A conditional F() update, so remaining_slots can never exceed total_slots
    even if a release is attempted twice. Returns the number of rows changed.
    """
    # A cancelled Task keeps remaining_slots at 0.
    return (Task.objects.filter(pk=task_id, remaining_slots__lt=F("total_slots"))
            .exclude(status=Task.Status.CANCELLED)
            .update(remaining_slots=F("remaining_slots") + 1, updated_at=now))


def sync_task_state(task_id, now=None):
    """Derive a Task's status, and its Order's statuses, from its claims and slots.

    Run inside the transaction that changed a claim or a slot, after that change:
    - COMPLETED only when approved claims == total_slots;
    - PAUSED stays paused until an admin resumes it;
    - FULLY_CLAIMED when no slot is free but held work is still open;
    - AVAILABLE otherwise (so a released slot reopens a fully claimed Task).
    """
    now = now or timezone.now()
    task = Task.objects.select_for_update().get(pk=task_id)
    if task.status in FINAL_TASK_STATUSES:
        return task
    counts = TaskClaim.objects.filter(task_id=task_id).aggregate(
        total=Count("id"),
        approved=Count("id", filter=Q(status=TaskClaim.Status.APPROVED)),
    )
    if counts["approved"] >= task.total_slots:
        status = Task.Status.COMPLETED
    elif task.status == Task.Status.PAUSED:
        status = Task.Status.PAUSED
    elif task.remaining_slots == 0:
        status = Task.Status.FULLY_CLAIMED
    else:
        status = Task.Status.AVAILABLE
    if status != task.status:
        Task.objects.filter(pk=task_id).update(status=status, updated_at=now)
        task.status = status
    if task.order_id:
        _sync_order(task, has_claims=bool(counts["total"]), now=now)
    return task


def _sync_order(task, *, has_claims, now):
    if task.status == Task.Status.COMPLETED:
        fulfillment, availability = Order.FulfillmentStatus.COMPLETED, Order.AvailabilityStatus.FULLY_CLAIMED
    else:
        fulfillment = Order.FulfillmentStatus.IN_PROGRESS if has_claims else Order.FulfillmentStatus.ACCEPTED
        if task.status == Task.Status.PAUSED:
            availability = Order.AvailabilityStatus.UNAVAILABLE
        elif task.status == Task.Status.FULLY_CLAIMED:
            availability = Order.AvailabilityStatus.FULLY_CLAIMED
        elif task.remaining_slots == task.total_slots:
            availability = Order.AvailabilityStatus.AVAILABLE
        else:
            availability = Order.AvailabilityStatus.PARTIALLY_CLAIMED
    (Order.objects.filter(pk=task.order_id)
        .exclude(fulfillment_status__in=FINAL_ORDER_STATUSES)
        .exclude(fulfillment_status=fulfillment, availability_status=availability)
        .update(fulfillment_status=fulfillment, availability_status=availability, updated_at=now))


def configured_claim_timeout_minutes():
    """The Admin-configured claim timeout (Site Configuration), or the field default."""
    value = SiteConfiguration.objects.filter(pk=1).values_list("claim_timeout_minutes", flat=True).first()
    return value or SiteConfiguration._meta.get_field("claim_timeout_minutes").default


def claim_expires_at(claim, timeout_minutes=None):
    """When a still-`claimed` claim expires; None for any other status."""
    if claim.status != TaskClaim.Status.CLAIMED or not claim.claimed_at:
        return None
    timeout = timedelta(minutes=timeout_minutes or configured_claim_timeout_minutes())
    return claim.claimed_at + timeout + claim.deadline_extension


def claim_clock(now):
    """The moment claim deadlines are measured against.

    While maintenance mode is on the clock stands still at the moment it began;
    ending maintenance gives each paused claim that time back (extend_paused_claims).
    """
    paused_at = (SiteConfiguration.objects.filter(pk=1, maintenance_enabled=True)
                 .values_list("maintenance_started_at", flat=True).first())
    return min(now, paused_at) if paused_at else now


def extend_paused_claims(started, now):
    """Move back the deadline of every claim a maintenance pause (started..now) held.

    Claims whose deadline had already passed when the pause began are left to
    expire. A claim made during the pause gets only the part it spent paused.
    Returns how many claims were extended.
    """
    pause = max(now - started, timedelta(0))
    active = TaskClaim.objects.filter(status=TaskClaim.Status.CLAIMED)
    not_due = started - timedelta(minutes=configured_claim_timeout_minutes()) - F("deadline_extension")
    extended = active.filter(claimed_at__lte=started, claimed_at__gte=not_due).update(
        deadline_extension=F("deadline_extension") + pause, updated_at=now)
    for claim_id, claimed_at in active.filter(claimed_at__gt=started).values_list("pk", "claimed_at"):
        extended += active.filter(pk=claim_id).update(
            deadline_extension=F("deadline_extension") + max(now - claimed_at, timedelta(0)), updated_at=now)
    return extended


def expire_stale_claims(now=None):
    """Expire every claim still `claimed` past its deadline (claim_expires_at).

    Each claim changes status through a conditional UPDATE in its own
    transaction, so concurrent sweeps (lazy calls and the expire_claims command)
    expire it, and release its slot, exactly once. Returns how many expired.
    """
    now = now or timezone.now()
    cutoff, stale = _retry_when_locked(_stale_claims, now=now)
    return sum(
        _retry_when_locked(_expire_claim, claim_id=claim_id, task_id=task_id, cutoff=cutoff, now=now)
        for claim_id, task_id in stale
    )


def _stale_claims(*, now):
    # A claim is stale once claimed_at + timeout + deadline_extension < the claim clock.
    cutoff = claim_clock(now) - timedelta(minutes=configured_claim_timeout_minutes())
    stale = TaskClaim.objects.filter(status=TaskClaim.Status.CLAIMED, claimed_at__lt=cutoff - F("deadline_extension"))
    return cutoff, list(stale.values_list("pk", "task_id"))


@transaction.atomic
def _expire_claim(*, claim_id, task_id, cutoff, now):
    changed = TaskClaim.objects.filter(
        pk=claim_id, status=TaskClaim.Status.CLAIMED, claimed_at__lt=cutoff - F("deadline_extension"),
    ).update(status=TaskClaim.Status.EXPIRED, active_for_user=None, updated_at=now)
    if changed:
        release_slot(task_id, now)
        sync_task_state(task_id, now)
    return changed


def _refuse_if_past_deadline(claim, now):
    """The deadline holds even if no sweep has expired the claim yet."""
    expires_at = claim_expires_at(claim)
    if expires_at and claim_clock(now) >= expires_at:
        raise ValidationError(CLAIM_EXPIRED_MESSAGE)


def _raise_not_active(task_id, user):
    if TaskClaim.objects.filter(task_id=task_id, user=user, status=TaskClaim.Status.EXPIRED).exists():
        raise ValidationError(CLAIM_EXPIRED_MESSAGE)
    raise ValidationError("This task is not actively assigned to you.")


def claim_task(*, task_id, user):
    if not getattr(user, "is_authenticated", False):
        raise ValidationError("You must be logged in to claim a task.")
    # Stale claims free their slots (and their owner's active-claim spot) first.
    expire_stale_claims()
    if user.approval_status == User.ApprovalStatus.DISAPPROVED:
        raise ValidationError("Your account was disapproved after reaching the strike limit and cannot claim tasks.")
    if not user.is_active or user.approval_status != User.ApprovalStatus.APPROVED:
        raise ValidationError("Your account is not approved to claim tasks.")
    # Every database check runs inside the retried transaction below.
    for attempt in range(5):
        try:
            return _claim_task_atomic(task_id=task_id, user=user)
        except IntegrityError:
            if TaskUserClaim.objects.filter(task_id=task_id, user_id=user.pk).exists():
                raise ValidationError("You have already claimed this task.")
            if TaskClaim.objects.filter(user_id=user.pk, status=TaskClaim.Status.CLAIMED).exists():
                raise ValidationError("You already have an active task. Finish it first.")
            raise ValidationError("A task claim could not be completed. Refresh and check your tasks.")
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 4:
                raise ValidationError("The task is being claimed right now. Refresh before trying again.")
            time.sleep(0.025 * (attempt + 1))


@transaction.atomic
def _claim_task_atomic(*, task_id, user):
    task = Task.objects.select_related("order__service").filter(pk=task_id).first()
    if not task:
        raise ValidationError("Task not found.")
    # Serialize this user's claims, and re-check a disapproval made after the
    # request began. TaskClaim.active_for_user (unique) is the database guard for
    # "one active claim per user"; this lock just turns races into a clear message.
    locked_user = get_user_model().objects.select_for_update().get(pk=user.pk)
    if not locked_user.is_active or locked_user.approval_status != User.ApprovalStatus.APPROVED:
        raise ValidationError("Your account is not approved to claim tasks.")
    if not user_can_access_task(user, task):
        raise ValidationError("Your current level does not have access to this task.")
    if TaskUserClaim.objects.filter(task_id=task_id, user_id=user.pk).exists():
        raise ValidationError("You have already claimed this task.")
    # Duplicate-link rule: no second Task for a link and service this user already worked on.
    refuse_if_worked_on_link(task=task, user_id=user.pk)
    if TaskClaim.objects.filter(user_id=user.pk, status=TaskClaim.Status.CLAIMED).exists():
        raise ValidationError("You already have an active task. Finish it first.")
    now = timezone.now()
    # The conditional UPDATE is the slot lock: two concurrent claims cannot both
    # consume the last slot, and capacity can never go below zero.
    changed = Task.objects.filter(pk=task_id, status=Task.Status.AVAILABLE, remaining_slots__gt=0).update(
        remaining_slots=F("remaining_slots") - 1, updated_at=now)
    if not changed:
        raise ValidationError("This task is no longer available.")
    task.refresh_from_db()
    # Both rows are unique-constrained; an IntegrityError rolls back the decrement.
    TaskUserClaim.objects.create(task=task, user=user)
    claim = TaskClaim.objects.create(task=task, user=user, active_for_user=user)
    # Taking the last slot makes the Task FULLY_CLAIMED, not COMPLETED: it is
    # completed only once every slot holds an approved claim.
    task = sync_task_state(task.pk, now)
    task.claim_id = claim.pk
    return task


def configured_target_wait_seconds():
    """The Admin-configured Open Target wait (Site Configuration), or the field default."""
    value = SiteConfiguration.objects.filter(pk=1).values_list("target_wait_seconds", flat=True).first()
    return value or SiteConfiguration._meta.get_field("target_wait_seconds").default


def claim_wait_seconds(claim):
    """The wait that applies to this claim: the value stored when its target was first opened.

    Before the first open (and for claims opened before the wait was stored) the
    current setting applies.
    """
    return claim.target_wait_seconds or configured_target_wait_seconds()


def claim_eligible_at(claim, wait_seconds=None):
    """When this claim may submit. ``wait_seconds`` lets a caller pass a value it already read."""
    if not claim.target_opened_at:
        return None
    return claim.target_opened_at + timedelta(seconds=wait_seconds or claim_wait_seconds(claim))


def remaining_wait_seconds(claim, now, wait_seconds=None):
    """Whole seconds (rounded up) until this claim may submit; None before the target is opened."""
    eligible_at = claim_eligible_at(claim, wait_seconds)
    if not eligible_at:
        return None
    return max(0, math.ceil((eligible_at - now).total_seconds()))


def _valid_target_url(value):
    """The absolute URL a worker opens, by the same rule orders are stored with; None if unusable."""
    try:
        return normalize_target_url(value) or None
    except ValidationError:
        return None


def open_task_target(*, task_id, user):
    """Record (once) that this user opened the target of their own active claim."""
    expire_stale_claims()
    claim = TaskClaim.objects.select_related("task").filter(
        task_id=task_id, user=user, status=TaskClaim.Status.CLAIMED).first()
    if not claim:
        _raise_not_active(task_id, user)
    _refuse_if_past_deadline(claim, timezone.now())
    target = _valid_target_url(claim.task.target_link)
    if not target:
        raise ValidationError("This task does not have a valid target link.")
    now = timezone.now()
    # Only the first open is recorded, together with the wait in effect at that
    # moment; repeated requests cannot restart, extend or shorten the wait.
    TaskClaim.objects.filter(pk=claim.pk, target_opened_at__isnull=True).update(
        target_opened_at=now, target_wait_seconds=configured_target_wait_seconds(), updated_at=now)
    claim.refresh_from_db(fields=["target_opened_at", "target_wait_seconds"])
    eligible_at = claim_eligible_at(claim)
    now = timezone.now()
    return {"claim_id": claim.pk, "target_url": target, "opened_at": claim.target_opened_at,
        "wait_seconds": claim_wait_seconds(claim), "eligible_at": eligible_at, "server_time": now,
        "remaining_wait_seconds": remaining_wait_seconds(claim, now),
        "submission_eligible": now >= eligible_at}


def submit_task_result(*, task_id, user, result, note=""):
    """
    Submit a claimed task for admin review (Work Done) or report that the
    target is not available (Target Not Available).
    """
    # Outside the submission transaction, so an expiry is kept even though the
    # submission itself is then refused.
    expire_stale_claims()
    return _submit_task_result(task_id=task_id, user=user, result=result, note=note)


@transaction.atomic
def _submit_task_result(*, task_id, user, result, note):
    if not getattr(user, "is_authenticated", False):
        raise ValidationError("You must be logged in.")

    if result not in TaskClaim.Result.values:
        raise ValidationError("Invalid submission result.")

    if not isinstance(note, str):
        raise ValidationError("Note must be text.")

    if len(note) > 2000:
        raise ValidationError("Note cannot exceed 2000 characters.")

    now = timezone.now()
    next_status = (
        TaskClaim.Status.WORK_SUBMITTED if result == TaskClaim.Result.WORK_DONE
        else TaskClaim.Status.POST_NOT_AVAILABLE
    )
    claim = TaskClaim.objects.filter(task_id=task_id, user=user, status=TaskClaim.Status.CLAIMED).first()
    if not claim:
        _raise_not_active(task_id, user)
    _refuse_if_past_deadline(claim, now)
    # Server-side gate: uses this claim's own persisted open time and server time only.
    if not claim.target_opened_at:
        raise ValidationError("Open the target before submitting.")
    if now < claim_eligible_at(claim):
        raise ValidationError(f"Wait at least {claim_wait_seconds(claim)} seconds after opening the target before submitting.")
    updated = TaskClaim.objects.filter(pk=claim.pk, status=TaskClaim.Status.CLAIMED).update(
        status=next_status,
        active_for_user=None,
        submitted_result=result,
        submission_note=note.strip(),
        submitted_at=now,
        updated_at=now,
    )
    if not updated:
        if not Task.objects.filter(pk=task_id).exists():
            raise ValidationError("Task not found.")
        if not TaskClaim.objects.filter(task_id=task_id, user=user).exists():
            raise ValidationError("This task is not actively assigned to you.")
        raise ValidationError("This task has already been submitted or closed.")
    if next_status == TaskClaim.Status.POST_NOT_AVAILABLE:
        # Target Not Available needs no review: no reward, no strike, and the
        # slot goes back so another user can try the target.
        release_slot(task_id, now)
        _pause_if_target_reported_unavailable(task_id, now)
        sync_task_state(task_id, now)
    claim = TaskClaim.objects.select_related("task").get(pk=claim.pk)
    claim.task.claim_id = claim.pk
    return claim.task


def _pause_if_target_reported_unavailable(task_id, now):
    """Pause the Task once its Target Not Available reports reach both limits.

    Limits (Site Configuration): at least tna_pause_min_reports reports, and at
    least tna_pause_percent % of the Task's submissions.
    """
    defaults = {name: SiteConfiguration._meta.get_field(name).default for name in ("tna_pause_min_reports", "tna_pause_percent")}
    config = SiteConfiguration.objects.filter(pk=1).values(*defaults).first() or defaults
    counts = TaskClaim.objects.filter(task_id=task_id, submitted_at__isnull=False).aggregate(
        submissions=Count("id"),
        reports=Count("id", filter=Q(status=TaskClaim.Status.POST_NOT_AVAILABLE)),
    )
    reports, submissions = counts["reports"], counts["submissions"]
    if reports >= config["tna_pause_min_reports"] and reports * 100 >= config["tna_pause_percent"] * submissions:
        paused = Task.objects.filter(pk=task_id, status__in=[Task.Status.AVAILABLE, Task.Status.FULLY_CLAIMED]).update(
            status=Task.Status.PAUSED, updated_at=now)
        if paused:
            send_admin_alert("task_paused", (
                f"Task #{task_id} was paused automatically: {reports} Target Not Available reports "
                f"out of {submissions} submissions. Keep or cancel it in Task History."
            ))


# Only a Work Done submission can be pending review, approved or rejected.
_RESULT_IMPLIED_BY_STATUS = {
    TaskClaim.Status.WORK_SUBMITTED: TaskClaim.Result.WORK_DONE,
    TaskClaim.Status.APPROVED: TaskClaim.Result.WORK_DONE,
    TaskClaim.Status.REJECTED: TaskClaim.Result.WORK_DONE,
    TaskClaim.Status.POST_NOT_AVAILABLE: TaskClaim.Result.POST_NOT_AVAILABLE,
}


def submitted_result_label(claim):
    """Readable submitted result for display.

    Older submissions did not store submitted_result; their status still records
    which result the user chose, so the label is taken from it. Stored data is
    never rewritten here.
    """
    if claim.submitted_result:
        return claim.get_submitted_result_display()
    implied = _RESULT_IMPLIED_BY_STATUS.get(claim.status)
    return implied.label if implied else None


def _retry_when_locked(operation, **kwargs):
    """Run a review or expiry transaction, retrying briefly if SQLite reports a write lock.

    A failed attempt is fully rolled back, and the pending-status check inside
    the transaction makes a retry unable to apply a review twice.
    """
    for attempt in range(5):
        try:
            return operation(**kwargs)
        except OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == 4 or transaction.get_connection().in_atomic_block:
                raise
            time.sleep(0.05 * (attempt + 1))


def approve_submitted_task(**kwargs):
    return _retry_when_locked(_approve_submitted_task, **kwargs)


def reject_submitted_task(**kwargs):
    return _retry_when_locked(_reject_submitted_task, **kwargs)


@transaction.atomic
def _approve_submitted_task(*, task_id, reviewer, review_note=""):
    if not reviewer.is_authenticated or not reviewer.is_staff:
        raise ValidationError("Only an admin can approve tasks.")

    if not reviewer.is_active:
        raise ValidationError("Inactive admins cannot approve tasks.")

    if not isinstance(review_note, str) or len(review_note) > 2000:
        raise ValidationError("Invalid review note.")

    now = timezone.now()
    updated = TaskClaim.objects.filter(pk=task_id, status=TaskClaim.Status.WORK_SUBMITTED).update(
        status=TaskClaim.Status.APPROVED,
        reviewed_by=reviewer,
        reviewed_at=now,
        review_note=review_note.strip(),
        updated_at=now,
    )
    if not updated:
        if not TaskClaim.objects.filter(pk=task_id).exists():
            raise ValidationError("Task submission not found.")
        raise ValidationError("Only submitted tasks can be approved.")
    claim = TaskClaim.objects.select_related("task", "user").get(pk=task_id)
    credit_approved_task_reward(claim_id=claim.pk)
    # The last approval of a fully claimed Task completes it and its Order.
    sync_task_state(claim.task_id, now)
    # In the same transaction: the referral this approval may complete is paid once, or not at all.
    on_claim_approved(claim.user_id, now)
    return claim


@transaction.atomic
def _reject_submitted_task(*, task_id, reviewer, reason=""):
    if (
        not reviewer.is_authenticated
        or not reviewer.is_active
        or not reviewer.is_staff
    ):
        raise ValidationError("Only an active admin can reject tasks.")

    if not isinstance(reason, str) or len(reason) > 500:
        raise ValidationError("The review note cannot exceed 500 characters.")

    reason = reason.strip()

    now = timezone.now()
    updated = TaskClaim.objects.filter(pk=task_id, status=TaskClaim.Status.WORK_SUBMITTED).update(
        status=TaskClaim.Status.REJECTED,
        reviewed_by=reviewer,
        reviewed_at=now,
        review_note=reason,
        updated_at=now,
    )
    if not updated:
        if not TaskClaim.objects.filter(pk=task_id).exists():
            raise ValidationError("Task submission not found.")
        raise ValidationError("Only submitted tasks can be rejected.")
    claim = TaskClaim.objects.select_related("task", "user").get(pk=task_id)
    # Lock the user first so concurrent rejections for the same user are
    # counted one after another against the strike limit.
    get_user_model().objects.select_for_update().get(pk=claim.user_id)
    # One strike per claim (unique claim FK); an error here rolls back the
    # rejection, the strike and any status change together.
    TaskStrike.objects.create(
        task=claim.task,
        claim=claim,
        user=claim.user,
        issued_by=reviewer,
        reason=reason,
    )
    claim.account_status = enforce_strike_limit(claim.user_id)
    # A rejected claim no longer holds its slot; another user may take it.
    release_slot(claim.task_id, now)
    sync_task_state(claim.task_id, now)
    return claim


class TaskStateConflict(ValidationError):
    """The Task is in a state that does not allow the requested admin action."""


def _require_admin(reviewer, action):
    if not reviewer.is_authenticated or not reviewer.is_active or not reviewer.is_staff:
        raise ValidationError(f"Only an active admin can {action} tasks.")


def resume_task(**kwargs):
    return _retry_when_locked(_resume_task, **kwargs)


def cancel_task(**kwargs):
    return _retry_when_locked(_cancel_task, **kwargs)


@transaction.atomic
def _resume_task(*, task_id, reviewer):
    """Keep a paused Task: it becomes available again, or fully claimed if no slot is free.

    Idempotent: resuming a Task that is already available or fully claimed
    changes nothing.
    """
    _require_admin(reviewer, "resume")
    task = Task.objects.select_for_update().filter(pk=task_id).first()
    if task is None:
        raise ValidationError("Task not found.")
    if task.status in (Task.Status.AVAILABLE, Task.Status.FULLY_CLAIMED):
        return task
    if task.status != Task.Status.PAUSED:
        raise TaskStateConflict(f"Only a paused Task can be resumed; this Task is {task.get_status_display().lower()}.")
    now = timezone.now()
    Task.objects.filter(pk=task_id).update(status=Task.Status.AVAILABLE, updated_at=now)
    return sync_task_state(task_id, now)


@transaction.atomic
def _cancel_task(*, task_id, reviewer, reason, note=""):
    """An admin cancels a Task (available, fully claimed or paused); see _cancel_task_records."""
    _require_admin(reviewer, "cancel")
    if reason not in ADMIN_CANCEL_REASONS:
        raise ValidationError("Choose a valid cancel reason.")
    note = (note or "").strip() if isinstance(note, str) else note
    if not isinstance(note, str) or len(note) > 2000:
        raise ValidationError("The note must be text of at most 2000 characters.")
    if reason == Task.CancelReason.OTHER and not note:
        raise ValidationError("Add a note explaining the reason.")
    return _cancel_task_records(task_id=task_id, reason=reason, note=note, cancelled_by=reviewer)


def _cancel_task_records(*, task_id, reason, note, cancelled_by):
    """Cancel a Task and settle its Order. Runs inside the caller's transaction.

    Approved claims, and the rewards credited for them, are kept. Claims still
    claimed or awaiting review become `cancelled`: no reward, no strike. The
    Task keeps no free slot, and its Order becomes `partial` when some work was
    approved, otherwise `cancelled`, and an API client's balance is refunded
    for the undelivered units (orders.admin_wallets.refund_api_order).
    Idempotent: an already-cancelled Task is returned unchanged.
    ``cancelled_by`` is None for the automatic expiry.
    """
    if not Task.objects.filter(pk=task_id).exists():
        raise ValidationError("Task not found.")

    now = timezone.now()
    open_claims = TaskClaim.objects.filter(task_id=task_id, status__in=OPEN_CLAIM_STATUSES)
    # Claims first, then the Task: the same lock order as reviews, submissions
    # and expiry (claim row, then Task row), so they cannot deadlock with this.
    open_claims.update(status=TaskClaim.Status.CANCELLED, active_for_user=None, updated_at=now)
    task = Task.objects.select_for_update().get(pk=task_id)
    if task.status == Task.Status.CANCELLED:
        return task
    if task.status == Task.Status.COMPLETED:
        raise TaskStateConflict("A completed Task cannot be cancelled; every slot is already approved.")
    # With the Task locked no new claim can start; catch any that committed meanwhile.
    open_claims.update(status=TaskClaim.Status.CANCELLED, active_for_user=None, updated_at=now)

    Task.objects.filter(pk=task_id).update(
        status=Task.Status.CANCELLED, remaining_slots=0, cancel_reason=reason, cancel_note=note,
        cancelled_at=now, cancelled_by=cancelled_by, updated_at=now,
    )
    if task.order_id:
        delivered = TaskClaim.objects.filter(task_id=task_id, status=TaskClaim.Status.APPROVED).count()
        Order.objects.filter(pk=task.order_id).update(
            fulfillment_status=Order.FulfillmentStatus.PARTIAL if delivered else Order.FulfillmentStatus.CANCELLED,
            availability_status=Order.AvailabilityStatus.UNAVAILABLE,
            updated_at=now,
        )
        # An API client gets the undelivered part back, in this same transaction (once).
        refund_api_order(task.order_id)
    return Task.objects.select_related("order", "cancelled_by").get(pk=task_id)


def configured_order_max_age_hours():
    """The Admin-configured order expiry age (Site Configuration), or the field default."""
    value = SiteConfiguration.objects.filter(pk=1).values_list("order_max_age_hours", flat=True).first()
    return value or SiteConfiguration._meta.get_field("order_max_age_hours").default


def expire_old_orders(now=None):
    """Cancel every Order still open after the configured maximum age.

    Each Order goes through the same cancel flow as an admin cancellation, with
    reason `expired` and no admin: approved work and its rewards stay, open
    claims are cancelled without strikes, and the Order ends Partial (some work
    approved) or Cancelled. One transaction per Order, so concurrent sweeps
    settle each Order once. Returns how many Orders were expired.
    """
    now = now or timezone.now()
    hours = configured_order_max_age_hours()
    cutoff = now - timedelta(hours=hours)
    order_ids = list(
        Order.objects.filter(created_at__lt=cutoff, fulfillment_status__in=EXPIRABLE_ORDER_STATUSES)
        .order_by("id").values_list("pk", flat=True)
    )
    return sum(_retry_when_locked(_expire_order, order_id=order_id, hours=hours) for order_id in order_ids)


@transaction.atomic
def _expire_order(*, order_id, hours):
    task_id = (Task.objects.filter(order_id=order_id).exclude(status=Task.Status.CANCELLED)
               .order_by("id").values_list("pk", flat=True).first())
    if task_id is None:
        return 0
    try:
        # A savepoint, so a refused cancellation leaves nothing half-applied.
        with transaction.atomic():
            task = _cancel_task_records(
                task_id=task_id, reason=Task.CancelReason.EXPIRED, note=f"Open longer than {hours} hours.", cancelled_by=None,
            )
    except TaskStateConflict:
        # Completed in the meantime: nothing to expire.
        return 0
    if task.cancel_reason != Task.CancelReason.EXPIRED:
        return 0
    order = Order.objects.only("fulfillment_status", "quantity").get(pk=order_id)
    delivered = TaskClaim.objects.filter(task_id=task.pk, status=TaskClaim.Status.APPROVED).count()
    outcome = "Partial" if order.fulfillment_status == Order.FulfillmentStatus.PARTIAL else "Canceled"
    send_admin_alert("order_expired", (
        f"Order #{order_id} expired after {hours} hours and is now {outcome} "
        f"({delivered} of {order.quantity} delivered)."
    ))
    return 1


def recent_target_not_available_rates(user_ids, window=20):
    """Each user's Target Not Available share of their last `window` submissions.

    Display only: nothing acts on it. Returns {user_id: {...}} for every id given.
    """
    from django.db.models import Window
    from django.db.models.functions import RowNumber

    ranked = TaskClaim.objects.filter(user_id__in=user_ids, submitted_at__isnull=False).annotate(
        recent_rank=Window(RowNumber(), partition_by=[F("user_id")], order_by=[F("submitted_at").desc(), F("id").desc()]),
    )
    totals = {user_id: [0, 0] for user_id in user_ids}
    for user_id, status in ranked.filter(recent_rank__lte=window).values_list("user_id", "status"):
        totals[user_id][0] += 1
        totals[user_id][1] += status == TaskClaim.Status.POST_NOT_AVAILABLE
    return {
        user_id: {
            "recent_submissions": submissions,
            "target_not_available": reports,
            "rate_percent": str((Decimal(reports * 100) / submissions).quantize(Decimal("0.1"))) if submissions else None,
            "flagged": bool(submissions) and reports * 2 >= submissions,
        }
        for user_id, (submissions, reports) in totals.items()
    }
