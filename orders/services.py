from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q

from accounts.models import UserLevel
from common.notifications import notify_new_order
from orders.admin_wallets import (
    InsufficientFunds, charge_for, check_order_funds, debit_order_charge, lock_wallet_for_order, record_refused_order,
)
from orders.duplicates import refuse_duplicate_active_order
from orders.links import canonical_link, normalize_target_url
from orders.models import Order
from services_catalog.models import Service
from services_catalog.calculations import calculate_max_order_quantity
from tasks.models import Task, TaskClaim
from tasks.services import _retry_when_locked, create_tasks_for_order


def get_eligible_user_count(service):
    """Count active, approved worker accounts eligible for this service.

    Administrators cannot claim work, so they never add capacity.
    """
    User = get_user_model()
    users = User.objects.filter(
        is_active=True,
        is_staff=False,
        is_superuser=False,
        approval_status=User.ApprovalStatus.APPROVED,
    )
    if service.eligible_level == Service.EligibleLevel.ALL:
        return users.count()

    selected_level = UserLevel.objects.filter(code=service.eligible_level, is_active=True).first()
    if not selected_level:
        raise ValidationError("The service's configured user level is missing or inactive.")

    users = users.annotate(
        approved_task_count=Count(
            "task_claims",
            filter=Q(task_claims__status=TaskClaim.Status.APPROVED),
        )
    ).filter(approved_task_count__gte=selected_level.minimum_approved_tasks)
    return users.count()


def live_capacity_limit(service):
    """The largest quantity the service accepts right now: eligible users x capacity %.

    0 when the service's level is missing or inactive (it cannot take orders).
    """
    try:
        return calculate_max_order_quantity(service, get_eligible_user_count(service))
    except ValidationError:
        return 0


def _same_order(order, service, quantity, target_link, source):
    return (
        order.service_id == service.pk
        and order.quantity == quantity
        and order.target_link == target_link
        and order.source == source
    )


def _ensure_retry_task(order):
    """A retried request never adds capacity; it only completes a missing Task for a live order."""
    if order.fulfillment_status == Order.FulfillmentStatus.CANCELLED:
        return
    create_tasks_for_order(order.pk)


def order_task_summary(order):
    """The single aggregate Task linked to an Order, as returned by order APIs."""
    task = order.tasks.order_by("id").first()
    if not task:
        return None
    return {
        "id": task.id,
        "status": task.status,
        "total_slots": task.total_slots,
        "remaining_slots": task.remaining_slots,
        "reward_per_approved_claim": str(task.reward_amount),
    }


@transaction.atomic
def _create_service_order(*, source, service_id, external_order_id, target_link, quantity, idempotency_key=None,
                          require_external_id=True, admin_wallet=None):
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ValidationError("Quantity must be a whole number greater than zero.")
    # API orders are always placed with an admin's key and paid from that admin's wallet; manual ones never are.
    if (source == Order.Source.API) != (admin_wallet is not None):
        raise ValidationError("An API order needs its admin wallet, and a manual order cannot have one.")
    if source == Order.Source.API:
        # Provider API (Perfect Panel) orders carry no external ID; each request is a new order.
        if require_external_id or external_order_id is not None:
            if not isinstance(external_order_id, str) or not external_order_id.strip():
                raise ValidationError("External order ID is required.")
            external_order_id = external_order_id.strip()
            if len(external_order_id) > Order._meta.get_field("external_order_id").max_length:
                raise ValidationError("External order ID is too long.")
    else:
        external_order_id = None
        if idempotency_key is None:
            raise ValidationError("A request key is required for manual order creation.")

    # Trimmed, https:// added when no scheme was given, and refused unless it is
    # an http(s) URL (javascript:, data:, ...), the same for every order source.
    target_link = normalize_target_url(target_link)

    try:
        service = Service.objects.select_for_update().get(pk=service_id)
    except Service.DoesNotExist:
        raise ValidationError("Service not found.")
    if not service.is_active:
        raise ValidationError("This service is currently inactive.")
    allowed_source = Service.OrderSource.API if source == Order.Source.API else Service.OrderSource.MANUAL
    if service.order_source not in (allowed_source, Service.OrderSource.BOTH):
        raise ValidationError(f"This service does not accept {allowed_source} orders.")
    if quantity < service.minimum_order_quantity:
        raise ValidationError(f"Minimum order quantity is {service.minimum_order_quantity}.")
    if quantity > service.max_order_quantity:
        raise ValidationError(f"Maximum order quantity is {service.max_order_quantity}.")
    if service.target_link_required and not target_link:
        raise ValidationError("A target link is required.")

    existing_order = None
    if external_order_id:
        # External order IDs are the wallet owner's own references: unique per wallet only.
        existing_order = Order.objects.filter(admin_wallet=admin_wallet, external_order_id=external_order_id).first()
    elif idempotency_key:
        existing_order = Order.objects.filter(idempotency_key=idempotency_key).first()
    if existing_order:
        if not _same_order(existing_order, service, quantity, target_link, source):
            raise ValidationError("This request key has already been used with different order details." if idempotency_key else "This external order ID has already been used with different order details.")
        _ensure_retry_task(existing_order)
        return existing_order, False

    # A retry above returns its own order; a new order for a link and service
    # that still has an open order is refused (the Service row is locked).
    refuse_duplicate_active_order(service=service, normalized_link=canonical_link(target_link))

    eligible_user_count = get_eligible_user_count(service)
    capacity_limit = calculate_max_order_quantity(service, eligible_user_count)
    if quantity > capacity_limit:
        raise ValidationError(f"Order quantity exceeds the service capacity. Maximum allowed quantity is {capacity_limit}.")

    order_value = service.price_per_1000 * quantity / 1000
    wallet, charge = None, None
    if admin_wallet is not None:
        # The wallet row stays locked until this order commits, so concurrent
        # orders are charged one after another and can never overdraw it.
        wallet = lock_wallet_for_order(admin_wallet.pk)
        charge = charge_for(order_value)
        check_order_funds(wallet, charge)

    try:
        with transaction.atomic():
            order = Order.objects.create(
                service=service,
                source=source,
                external_order_id=external_order_id,
                idempotency_key=idempotency_key,
                target_link=target_link,
                target_type_snapshot=service.target_type,
                quantity=quantity,
                price_per_1000_snapshot=service.price_per_1000,
                fulfillment_cost_per_1000_snapshot=service.fulfillment_cost_per_1000,
                has_fulfillment_cost_snapshot=service.has_fulfillment_cost,
                business_share_percentage_snapshot=service.business_share_percentage,
                user_reward_percentage_snapshot=service.user_reward_percentage,
                capacity_percentage_snapshot=service.capacity_percentage,
                eligible_user_count_at_acceptance=eligible_user_count,
                capacity_limit_at_acceptance=capacity_limit,
                total_order_value=order_value,
                availability_status=Order.AvailabilityStatus.AVAILABLE,
                fulfillment_status=Order.FulfillmentStatus.ACCEPTED,
                admin_wallet=wallet,
                charge_amount=charge,
            )
    except IntegrityError:
        # Without an external ID or request key there is no earlier request to match.
        lookup = {"admin_wallet": admin_wallet, "external_order_id": external_order_id} if external_order_id else (
            {"idempotency_key": idempotency_key} if idempotency_key else None
        )
        existing_order = Order.objects.filter(**lookup).first() if lookup else None
        if existing_order and _same_order(existing_order, service, quantity, target_link, source):
            _ensure_retry_task(existing_order)
            return existing_order, False
        raise ValidationError("The order could not be created. Check the external order ID or request key.")

    # The charge, the order and its Task are one transaction: any failure rolls back all three.
    if wallet is not None:
        debit_order_charge(wallet, order)
    create_tasks_for_order(order.pk)
    notify_new_order(order)
    return order, True


def _charged_order(admin_wallet, **kwargs):
    """An API order paid from `admin_wallet`; a refusal for funds is counted for the alert."""
    try:
        order, _ = _retry_when_locked(_create_service_order, source=Order.Source.API, admin_wallet=admin_wallet, **kwargs)
    except InsufficientFunds:
        # The order's transaction has rolled back; the count is kept outside it.
        record_refused_order(admin_wallet.pk)
        raise
    return order


def create_api_order(*, admin_wallet, service_id, external_order_id, target_link, quantity):
    """Create an incoming API order with one aggregate Task whose capacity is the quantity."""
    return _charged_order(
        admin_wallet,
        service_id=service_id,
        external_order_id=external_order_id,
        target_link=target_link,
        quantity=quantity,
    )


def create_provider_order(*, admin_wallet, service_id, target_link, quantity):
    """A provider API (SMM API v2) order: the API order rules, without an external order ID."""
    return _charged_order(
        admin_wallet,
        service_id=service_id,
        external_order_id=None,
        target_link=target_link,
        quantity=quantity,
        require_external_id=False,
    )


def create_admin_order(*, service_id, target_link, quantity, idempotency_key):
    """Create a manual admin order using the same service, capacity, pricing and task rules."""
    return _create_service_order(
        source=Order.Source.MANUAL,
        service_id=service_id,
        external_order_id=None,
        target_link=target_link,
        quantity=quantity,
        idempotency_key=idempotency_key,
    )
