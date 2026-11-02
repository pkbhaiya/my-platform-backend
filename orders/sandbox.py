"""Sandbox orders: test orders placed with a sandbox wallet's key (AdminWallet.is_sandbox).

They pass the same checks as any API order and are charged to the sandbox
wallet, but they have no Task, so no worker ever sees them, and analytics and
dashboards leave them out. So that a panel can test every status, they finish
on their own, moved on by the every-minute expire_claims run (or
advance_sandbox_orders): Pending, In progress 1 minute after creation, and 3
minutes after creation Completed (even quantity) or Partial with remains =
SANDBOX_PARTIAL_REMAINS (odd quantity), whose undelivered unit is refunded to
the wallet like any Partial order's.
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from tasks.services import _retry_when_locked

from .admin_wallets import refund_api_order
from .models import Order

IN_PROGRESS_AFTER = timedelta(minutes=1)
FINISHED_AFTER = timedelta(minutes=3)
OPEN_STATUSES = (Order.FulfillmentStatus.ACCEPTED, Order.FulfillmentStatus.IN_PROGRESS)


def advance_sandbox_orders(now=None):
    """Move every due sandbox order on. Returns how many changed; safe to run concurrently."""
    now = now or timezone.now()
    sandbox = Order.objects.filter(is_sandbox=True)
    started = sandbox.filter(
        fulfillment_status=Order.FulfillmentStatus.ACCEPTED,
        created_at__lte=now - IN_PROGRESS_AFTER,
        created_at__gt=now - FINISHED_AFTER,
    ).update(
        fulfillment_status=Order.FulfillmentStatus.IN_PROGRESS,
        availability_status=Order.AvailabilityStatus.PARTIALLY_CLAIMED,
        updated_at=now,
    )
    due = list(sandbox.filter(fulfillment_status__in=OPEN_STATUSES, created_at__lte=now - FINISHED_AFTER)
               .order_by("id").values_list("pk", flat=True))
    return started + sum(_retry_when_locked(_finish_sandbox_order, order_id=order_id, now=now) for order_id in due)


@transaction.atomic
def _finish_sandbox_order(*, order_id, now):
    order = Order.objects.select_for_update().filter(pk=order_id, is_sandbox=True, fulfillment_status__in=OPEN_STATUSES).first()
    if order is None:
        return 0  # finished by a concurrent run
    partial = order.quantity % 2 == 1
    Order.objects.filter(pk=order.pk).update(
        fulfillment_status=Order.FulfillmentStatus.PARTIAL if partial else Order.FulfillmentStatus.COMPLETED,
        availability_status=Order.AvailabilityStatus.UNAVAILABLE if partial else Order.AvailabilityStatus.FULLY_CLAIMED,
        updated_at=now,
    )
    if partial:
        # In this same transaction, exactly once, like every Partial API order.
        refund_api_order(order.pk)
    return 1
