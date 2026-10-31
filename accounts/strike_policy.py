"""Per-level strike limits and automatic account disapproval.

A user's level is derived from their approved claims (highest active level whose
threshold they meet). Each level stores its own ``strike_limit``. When an admin
rejection gives an Approved user a strike count >= their current level's limit,
the account becomes Disapproved inside the same database transaction.
"""

from django.db.models import Count, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from .models import User, UserLevel

# Used only when no level exists at all, so a missing configuration can never
# mean "unlimited strikes".
FALLBACK_STRIKE_LIMIT = 3


def approved_claim_count(user_id):
    from tasks.models import TaskClaim
    return TaskClaim.objects.filter(user_id=user_id, status=TaskClaim.Status.APPROVED).count()


def strike_count(user_id):
    from tasks.models import TaskStrike
    return TaskStrike.objects.filter(user_id=user_id).count()


def active_levels():
    return list(UserLevel.objects.filter(is_active=True).order_by("minimum_approved_tasks", "display_order"))


def level_for_count(approved_count, levels=None):
    """Highest active level whose threshold the approved-claim count meets."""
    current = None
    for level in levels if levels is not None else active_levels():
        if level.is_active and approved_count >= level.minimum_approved_tasks:
            current = level
    return current


def per_user_count(queryset, field="user"):
    """Correlated COUNT for ``queryset`` rows whose ``field`` is the outer User row.

    Unlike two ``Count`` joins, this does not multiply rows (claims x strikes)
    and is dropped from the paginator's COUNT query.
    """
    counts = queryset.filter(**{field: OuterRef("pk")}).order_by().values(field).annotate(total=Count("pk")).values("total")
    return Coalesce(Subquery(counts[:1]), 0)


def level_count_filters(levels, field="approved_count"):
    """``[(level, Q)]`` matching ``level_for_count`` for an annotated approved-claim count.

    ``levels`` must come from ``active_levels()`` (sorted by threshold): each level
    covers [its threshold, the next level's threshold). Lets callers bucket users
    by level in one aggregate query instead of one query per user.
    """
    buckets = []
    for index, level in enumerate(levels):
        condition = Q(**{f"{field}__gte": level.minimum_approved_tasks})
        if index + 1 < len(levels):
            condition &= Q(**{f"{field}__lt": levels[index + 1].minimum_approved_tasks})
        buckets.append((level, condition))
    return buckets


def no_level_filter(levels, field="approved_count"):
    """Users below the lowest active threshold, i.e. ``level_for_count`` returns None."""
    return Q(**{f"{field}__lt": levels[0].minimum_approved_tasks}) if levels else Q()


def strike_limit_for(level, all_levels=None):
    """Return ``(limit, source)`` for a level.

    Without an applicable level the most restrictive configured limit applies;
    with no levels configured at all, ``FALLBACK_STRIKE_LIMIT`` does.
    """
    if level is not None:
        return level.strike_limit, "level"
    limits = [item.strike_limit for item in (all_levels if all_levels is not None else UserLevel.objects.all())]
    if limits:
        return min(limits), "most_restrictive_level"
    return FALLBACK_STRIKE_LIMIT, "fallback"


def strike_summary(user, *, levels=None, all_levels=None, strikes=None, approved=None):
    """Level, strike count and limit for display. Callers may pass prefetched values."""
    approved = approved_claim_count(user.pk) if approved is None else approved
    strikes = strike_count(user.pk) if strikes is None else strikes
    level = level_for_count(approved, levels)
    limit, source = strike_limit_for(level, all_levels)
    return {
        "approved_task_count": approved,
        "strike_count": strikes,
        "strike_limit": limit,
        "strike_limit_source": source,
        "strike_limit_reached": strikes >= limit,
        "current_level": (
            {"id": level.id, "code": level.code, "name": level.name, "badge_color": level.badge_color}
            if level else None
        ),
    }


def enforce_strike_limit(user_id):
    """Disapprove an Approved user whose strikes reached their level's limit.

    Must run inside the transaction that created the strike, after locking the
    user row, so concurrent rejections are evaluated one after another. Only an
    Approved, non-staff account is changed; nothing here ever re-approves.
    Returns the user's resulting approval status.
    """
    user = User.objects.select_for_update().get(pk=user_id)
    if user.approval_status != User.ApprovalStatus.APPROVED or user.is_staff or user.is_superuser:
        return user.approval_status
    summary = strike_summary(user)
    if not summary["strike_limit_reached"]:
        return user.approval_status
    level_name = summary["current_level"]["name"] if summary["current_level"] else "no level"
    reason = (
        f"Strike limit reached: {summary['strike_count']} strike(s) at {level_name} "
        f"(limit {summary['strike_limit']})."
    )
    User.objects.filter(pk=user_id, approval_status=User.ApprovalStatus.APPROVED).update(
        approval_status=User.ApprovalStatus.DISAPPROVED,
        status_reason=reason[:255],
        status_change_source=User.StatusChangeSource.STRIKE_LIMIT,
        status_changed_at=timezone.now(),
        status_changed_by=None,
    )
    return User.ApprovalStatus.DISAPPROVED
