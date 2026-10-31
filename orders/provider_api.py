"""SMM API v2 provider endpoint, compatible with Perfect Panel.

POST /api/v2 with ``key`` and ``action`` as form fields or JSON. Every reply is
HTTP 200 JSON; a refused request is ``{"error": "<message>"}``. The key is an
admin's personal key (compared as a hash in constant time, never logged): orders
are paid from that admin's wallet, `balance` is that wallet's balance, and
`status` sees only the orders charged to it. Orders go through
``orders.services.create_provider_order``: the same service rules, capacity
check, row locks and Task creation as every other order.
"""

import logging
import math
import re
import time
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework.exceptions import APIException, MethodNotAllowed, Throttled
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import BaseThrottle, SimpleRateThrottle
from rest_framework.views import APIView

from common.analytics import orders_with_delivery
from common.maintenance import MAINTENANCE_API_ERROR, accepting_api_orders
from services_catalog.models import Service

from .admin_wallets import reported_charge
from .api_keys import authenticate_admin_wallet
from .models import AdminWallet, Order
from .services import create_provider_order

logger = logging.getLogger("orders.provider_api")

CURRENCY = "INR"
MAX_STATUS_ORDERS = 100
CENTS = Decimal("0.01")
RATE_PLACES = Decimal("0.0001")
WHOLE_NUMBER = re.compile(r"[0-9]{1,12}")

# Order.fulfillment_status -> SMM API status.
STATUS_LABELS = {
    Order.FulfillmentStatus.ACCEPTED: "Pending",
    Order.FulfillmentStatus.IN_PROGRESS: "In progress",
    Order.FulfillmentStatus.UNDER_REVIEW: "In progress",
    Order.FulfillmentStatus.COMPLETED: "Completed",
    Order.FulfillmentStatus.PARTIAL: "Partial",
    Order.FulfillmentStatus.CANCELLED: "Canceled",
    Order.FulfillmentStatus.REJECTED: "Canceled",
}
CLAIMED_AVAILABILITY = (Order.AvailabilityStatus.PARTIALLY_CLAIMED, Order.AvailabilityStatus.FULLY_CLAIMED)


class ProviderError(Exception):
    """A refused request; its message is returned as {"error": message}."""


class ProviderAPIThrottle(SimpleRateThrottle):
    """Requests per client IP (DEFAULT_THROTTLE_RATES["provider_api"]; NUM_PROXIES decides the IP)."""

    scope = "provider_api"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


def text_param(data, name):
    """A request field as stripped text: "" when absent, None when it is not a scalar."""
    value = data.get(name, "")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return value.strip() if isinstance(value, str) else None


def whole_number(text):
    return int(text) if isinstance(text, str) and WHOLE_NUMBER.fullmatch(text) else None


def charge(order):
    """The amount debited from the wallet for the order (its value ROUND_HALF_UP to 2 dp).

    The same figure as the order_charge ledger row. A Partial, Completed or
    Canceled order reports its full original charge: the panel computes its
    own refund from ``remains``, as this platform does for the wallet.
    """
    return format(reported_charge(order), "f")


def order_status(order):
    """The SMM API status object for an Order annotated with ``delivered`` (approved claims)."""
    status = STATUS_LABELS.get(order.fulfillment_status, "Pending")
    if status == "Pending" and order.availability_status in CLAIMED_AVAILABILITY:
        status = "In progress"
    if status == "Completed":
        remains = 0
    elif status == "Canceled":
        remains = order.quantity
    else:
        remains = max(0, order.quantity - order.delivered)
    return {"charge": charge(order), "start_count": "0", "status": status, "remains": str(remains), "currency": CURRENCY}


def provider_orders(wallet, order_ids):
    """The wallet's own Orders by id; other wallets' and manual (admin) orders are never visible."""
    return {order.pk: order for order in orders_with_delivery().filter(pk__in=order_ids, admin_wallet=wallet)}


@method_decorator(sensitive_post_parameters("key"), name="dispatch")
class ProviderAPIView(APIView):
    authentication_classes = []
    permission_classes = []
    parser_classes = [FormParser, MultiPartParser, JSONParser]
    renderer_classes = [JSONRenderer]
    throttle_classes = [ProviderAPIThrottle]

    def post(self, request):
        started = time.monotonic()
        data = request.data if isinstance(request.data, dict) else {}
        action = text_param(data, "action") or ""
        details = {}
        try:
            if settings.PROVIDER_API_REQUIRE_HTTPS and not request.is_secure():
                raise ProviderError("HTTPS is required")
            if not data:
                raise ProviderError("Incorrect request")
            wallet = authenticate_admin_wallet(text_param(data, "key") or "")
            if wallet is None:
                raise ProviderError("Invalid API key")
            details["wallet"] = wallet.pk
            handler = {"services": self.services, "add": self.add, "status": self.status, "balance": self.balance}.get(action)
            if handler is None:
                raise ProviderError("Incorrect action")
            body = handler(wallet, data, details)
            outcome = "ok"
        except ProviderError as exc:
            body, outcome = {"error": str(exc)}, str(exc)
        except Exception:
            logger.exception("provider_api action=%s failed", action or "-")
            body, outcome = {"error": "Internal error. Please try again later."}, "internal error"
        self.log(request, action, outcome, started, details)
        return Response(body)

    def services(self, wallet, data, details):
        services = Service.objects.filter(
            is_active=True, order_source__in=(Service.OrderSource.API, Service.OrderSource.BOTH),
        ).order_by("service_id")
        rows = [{
            "service": service.service_id,
            "name": service.name,
            "type": "Default",
            "category": service.category,
            "rate": format(service.price_per_1000.quantize(RATE_PLACES), "f"),
            "min": str(service.minimum_order_quantity),
            "max": str(service.max_order_quantity),
            "refill": False,
            "cancel": False,
            "dripfeed": False,
        } for service in services]
        details["services"] = len(rows)
        return rows

    def add(self, wallet, data, details):
        if not accepting_api_orders():
            raise ProviderError(MAINTENANCE_API_ERROR)
        if text_param(data, "runs") or text_param(data, "interval"):
            raise ProviderError("Drip-feed orders are not supported")
        service_id = whole_number(text_param(data, "service"))
        quantity = whole_number(text_param(data, "quantity"))
        link = text_param(data, "link")
        details.update(service=service_id, quantity=quantity)
        if not service_id:
            raise ProviderError("Incorrect service ID")
        if not quantity:
            raise ProviderError("Quantity must be a whole number greater than zero.")
        if link is None:
            raise ProviderError("Incorrect link")
        try:
            order = create_provider_order(admin_wallet=wallet, service_id=service_id, target_link=link, quantity=quantity)
        except DjangoValidationError as exc:
            raise ProviderError(" ".join(exc.messages))
        details["order"] = order.pk
        return {"order": order.pk}

    def status(self, wallet, data, details):
        many = text_param(data, "orders")
        if many:
            raw_ids = [part.strip() for part in many.split(",") if part.strip()]
            if len(raw_ids) > MAX_STATUS_ORDERS:
                raise ProviderError(f"Send at most {MAX_STATUS_ORDERS} order IDs")
            found = provider_orders(wallet, [order_id for order_id in map(whole_number, raw_ids) if order_id])
            details["orders"] = len(raw_ids)
            return {
                raw: order_status(found[whole_number(raw)]) if whole_number(raw) in found else {"error": "Incorrect order ID"}
                for raw in raw_ids
            }
        order_id = whole_number(text_param(data, "order"))
        found = provider_orders(wallet, [order_id]) if order_id else {}
        details["order"] = order_id
        if order_id not in found:
            raise ProviderError("Incorrect order ID")
        return order_status(found[order_id])

    def balance(self, wallet, data, details):
        """The admin wallet's real balance (read now, not from the authenticated row)."""
        value = AdminWallet.objects.values_list("balance", flat=True).get(pk=wallet.pk)
        return {"balance": format(value.quantize(CENTS), "f"), "currency": CURRENCY}

    def handle_exception(self, exc):
        """Parse errors, wrong methods and throttling still answer 200 with {"error": ...}."""
        if not isinstance(exc, APIException):
            return super().handle_exception(exc)
        retry_after = None
        if isinstance(exc, Throttled):
            retry_after = math.ceil(exc.wait) if exc.wait else None
            message = f"Too many requests. Try again in {retry_after} seconds." if retry_after else "Too many requests."
        elif isinstance(exc, MethodNotAllowed):
            message = "Incorrect request method. Use POST."
        else:
            message = "Incorrect request"
        self.log(self.request, "-", message, None, {})
        response = Response({"error": message})
        if retry_after:
            response["Retry-After"] = str(retry_after)
        return response

    def log(self, request, action, outcome, started, details):
        elapsed = f" ms={round((time.monotonic() - started) * 1000)}" if started else ""
        extra = "".join(f" {key}={value}" for key, value in details.items())
        logger.info("provider_api action=%s ip=%s outcome=%s%s%s", action or "-", BaseThrottle().get_ident(request), outcome, elapsed, extra)
