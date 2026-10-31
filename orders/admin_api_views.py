from django.core.exceptions import ValidationError as DjangoValidationError
from django.shortcuts import get_object_or_404
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .duplicates import ACTIVE_ORDER_STATUSES, previous_orders_on_link
from .models import Order
from .services import create_admin_order, order_task_summary

# An API key, or anything about it, must never be stored by a browser or proxy.
NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "Pragma": "no-cache",
    "Expires": "0",
}


def no_store(response):
    for name, value in NO_STORE_HEADERS.items():
        response[name] = value
    return response


class AdminCreateOrderSerializer(serializers.Serializer):
    service_id = serializers.IntegerField(min_value=1)
    target_link = serializers.CharField(required=False, allow_blank=True, max_length=2048, default="")
    quantity = serializers.IntegerField(min_value=1)
    idempotency_key = serializers.UUIDField()


class AdminOrderCreateAPIView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request):
        serializer = AdminCreateOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            order, created = create_admin_order(**serializer.validated_data)
        except DjangoValidationError as exc:
            raise ValidationError({"detail": exc.messages})
        payload = {
            "order": order.pk,
            "external_order_id": order.external_order_id,
            "service_id": order.service_id,
            "quantity": order.quantity,
            "status": order.fulfillment_status,
            "total_order_value": str(order.total_order_value),
            "task_count": order.tasks.count(),
            "task": order_task_summary(order),
            "created": created,
        }
        return Response(payload, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class AdminOrderPreviousOrdersAPIView(APIView):
    """GET: orders on the same link (normalized) created before this one, any service, newest first."""

    permission_classes = [IsAdminUser]

    def get(self, request, order_id):
        order = get_object_or_404(Order, pk=order_id)
        return Response({
            "order": order.pk,
            "normalized_link": order.normalized_link,
            "results": [{
                "id": earlier.pk,
                "service_id": earlier.service_id,
                "service_name": earlier.service.name,
                "same_service": earlier.service_id == order.service_id,
                "status": earlier.fulfillment_status,
                "status_display": earlier.get_fulfillment_status_display(),
                "active": earlier.fulfillment_status in ACTIVE_ORDER_STATUSES,
                "quantity": earlier.quantity,
                "created_at": earlier.created_at.isoformat(),
                "updated_at": earlier.updated_at.isoformat(),
            } for earlier in previous_orders_on_link(order)],
        })
