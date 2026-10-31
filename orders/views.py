from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from services_catalog.calculations import calculate_max_order_quantity
from services_catalog.models import Service

from .permissions import HasIncomingOrderAPIKey
from .serializers import IncomingOrderSerializer
from .services import create_api_order, get_eligible_user_count, order_task_summary


class IncomingServicesAPIView(APIView):
    permission_classes = [HasIncomingOrderAPIKey]
    authentication_classes = []

    def get(self, request):
        services = Service.objects.filter(
            is_active=True,
            order_source__in=(Service.OrderSource.API, Service.OrderSource.BOTH),
        ).order_by("service_id")
        # Many services share an eligibility level; count each level's users once.
        eligible_counts = {}

        def maximum_quantity(service):
            if service.eligible_level not in eligible_counts:
                try:
                    eligible_counts[service.eligible_level] = get_eligible_user_count(service)
                except DjangoValidationError:
                    # Level missing or inactive: list the service, but it cannot take orders.
                    eligible_counts[service.eligible_level] = 0
            return calculate_max_order_quantity(service, eligible_counts[service.eligible_level])

        return Response([{
            "service_id": service.service_id,
            "name": service.name,
            "description": service.description,
            "rate": str(service.price_per_1000),
            "rate_unit": "INR per 1,000 units",
            "minimum_quantity": service.minimum_order_quantity,
            "maximum_quantity": maximum_quantity(service),
            "target_link_required": service.target_link_required,
        } for service in services])


class IncomingOrderAPIView(APIView):
    """
    Accept and validate incoming reseller API orders (X-API-Key authenticated).
    """

    permission_classes = [HasIncomingOrderAPIKey]
    authentication_classes = []

    def post(self, request):
        serializer = IncomingOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            order = create_api_order(
                admin_wallet=request.admin_wallet,
                service_id=serializer.validated_data["service_id"],
                external_order_id=(
                    serializer.validated_data["external_order_id"]
                ),
                target_link=serializer.validated_data["target_link"],
                quantity=serializer.validated_data["quantity"],
            )
        except DjangoValidationError as exc:
            raise ValidationError({"detail": exc.messages})

        return Response(
            {
                "order": order.pk,
                "external_order_id": order.external_order_id,
                "service_id": order.service_id,
                "quantity": order.quantity,
                "status": order.fulfillment_status,
                "total_order_value": str(order.total_order_value),
                # Debited from the admin's wallet (2 dp); null for an order placed before balances.
                "charge_amount": None if order.charge_amount is None else str(order.charge_amount),
                # One aggregate Task per Order; quantity is its claim capacity.
                "task_count": order.tasks.count(),
                "task": order_task_summary(order),
            },
            status=status.HTTP_200_OK,
        )
