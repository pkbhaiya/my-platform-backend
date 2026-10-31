"""Duplicate-link protection: orders and Tasks are matched by (Order.normalized_link, service).

Rule 1: while an order for a link and service is still open, a new order for
the same pair is refused (Site Configuration can switch this off).
Rule 2: once it is final a new order is accepted, but a worker who has any
claim on a Task for that link and service never sees or claims the new Task.
A different service on the same link (likes, then comments) is a different pair.
An order without a link is never matched.
"""

from django.core.exceptions import ValidationError
from django.db.models import Exists, OuterRef, Q

from common.models import SiteConfiguration
from tasks.models import TaskClaim

from .models import Order

DUPLICATE_ACTIVE_ORDER = "You have an active order with this link. Please wait until it is completed."
ALREADY_WORKED_ON_LINK = "This Task is not available to you: you already worked on this link for this service."
# Open orders; completed, partial, cancelled and rejected are final.
ACTIVE_ORDER_STATUSES = (
    Order.FulfillmentStatus.ACCEPTED, Order.FulfillmentStatus.IN_PROGRESS, Order.FulfillmentStatus.UNDER_REVIEW,
)


class DuplicateActiveOrder(ValidationError):
    def __init__(self):
        super().__init__(DUPLICATE_ACTIVE_ORDER)


def duplicate_blocking_enabled():
    value = SiteConfiguration.objects.filter(pk=1).values_list("block_duplicate_active_orders", flat=True).first()
    return SiteConfiguration._meta.get_field("block_duplicate_active_orders").default if value is None else value


def refuse_duplicate_active_order(*, service, normalized_link):
    """Rule 1. Call with the Service row locked (select_for_update): every order
    for that service waits on the same lock, so two orders for one (link,
    service) can never both pass this check."""
    if not normalized_link or not duplicate_blocking_enabled():
        return
    if Order.objects.filter(service=service, normalized_link=normalized_link, fulfillment_status__in=ACTIVE_ORDER_STATUSES).exists():
        raise DuplicateActiveOrder()


def worked_on_same_link(user, task_ref="pk", link_ref="order__normalized_link", service_ref="order__service_id"):
    """Rule 2 for a Task queryset: an Exists that is true when `user` has any claim
    on another Task whose order has the same link and service."""
    return Exists(
        TaskClaim.objects.filter(
            user=user,
            task__order__normalized_link=OuterRef(link_ref),
            task__order__service_id=OuterRef(service_ref),
        ).exclude(task_id=OuterRef(task_ref))
    )


def hide_tasks_already_worked(tasks, user):
    """Leave out Tasks whose link and service this user already worked on (one query, no per-row lookups)."""
    return tasks.exclude(Q(worked_on_same_link(user)) & ~Q(order__normalized_link=""))


def refuse_if_worked_on_link(*, task, user_id):
    """Rule 2 at claim time."""
    order = task.order
    if order is None or not order.normalized_link:
        return
    if TaskClaim.objects.filter(
        user_id=user_id, task__order__normalized_link=order.normalized_link, task__order__service_id=order.service_id,
    ).exclude(task_id=task.pk).exists():
        raise ValidationError(ALREADY_WORKED_ON_LINK)


def previous_orders_on_link(order, limit=50):
    """Orders on the same link created before this one, any service, newest first."""
    if not order.normalized_link:
        return Order.objects.none()
    earlier = Q(created_at__lt=order.created_at) | Q(created_at=order.created_at, pk__lt=order.pk)
    return (Order.objects.filter(earlier, normalized_link=order.normalized_link).exclude(pk=order.pk)
            .select_related("service").order_by("-created_at", "-pk")[:limit])
