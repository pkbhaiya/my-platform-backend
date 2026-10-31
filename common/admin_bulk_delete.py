import logging

from django.db import transaction
from rest_framework import serializers, status
from rest_framework.exceptions import ValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from orders.admin_wallets import refund_api_order
from orders.models import Order
from services_catalog.models import Service
from tasks.models import Task

logger = logging.getLogger(__name__)


class BulkDeleteInputSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1),
        allow_empty=False,
        max_length=100,
    )
    dry_run = serializers.BooleanField(default=False, required=False)

    def validate_ids(self, ids):
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Duplicate IDs are not allowed.")
        return ids


class AdminBulkDeleteAPIView(APIView):
    permission_classes = [IsAdminUser]
    model = None

    def post(self, request):
        serializer = BulkDeleteInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        dry_run = serializer.validated_data["dry_run"]
        results = [self.process_one(record_id, dry_run=dry_run) for record_id in serializer.validated_data["ids"]]
        counts = {
            outcome: sum(1 for item in results if item["result"] == outcome)
            for outcome in ("deleted", "archived", "skipped", "failed")
        }
        return Response({**counts, "dry_run": dry_run, "results": results}, status=status.HTTP_200_OK)

    def process_one(self, record_id, *, dry_run=False):
        try:
            with transaction.atomic():
                try:
                    record = self.model.objects.select_for_update().get(pk=record_id)
                except self.model.DoesNotExist:
                    return {"id": record_id, "result": "skipped", "reason": "Record no longer exists."}
                return self.process_record(record, dry_run=dry_run)
        except Exception:
            # The admin sees a generic reason; the real cause goes to the server log.
            logger.exception("Bulk delete of %s #%s failed", self.model.__name__, record_id)
            return {"id": record_id, "result": "failed", "reason": "The record could not be changed because of a database constraint or error."}

    def process_record(self, record, *, dry_run=False):
        raise NotImplementedError


def task_history_reason(task):
    """Return why a Task must be preserved, or None when it has no dependent history."""
    if task.claims.exists() or task.claiming_users.exists():
        return f"Task #{task.pk} has user claims; claim and review history is preserved."
    if task.strikes.exists():
        return f"Task #{task.pk} has strike history that must be preserved."
    if task.wallet_transactions.exists():
        return f"Task #{task.pk} has wallet ledger entries that must be preserved."
    return None


class AdminBulkDeleteOrdersAPIView(AdminBulkDeleteAPIView):
    """Delete an Order together with its unclaimed aggregate Task; skip Orders with history."""

    model = Order

    def process_record(self, order, *, dry_run=False):
        if order.admin_wallet_transactions.exists():
            return {"id": order.pk, "result": "skipped",
                    "reason": "The Order was charged to an admin wallet; its ledger entries are preserved, so the Order is kept."}
        tasks = list(order.tasks.all())
        for task in tasks:
            reason = task_history_reason(task)
            if reason:
                return {"id": order.pk, "result": "skipped", "reason": f"{reason} The Order is kept."}
        order_id = order.pk
        task_ids = ", ".join(f"#{task.pk}" for task in tasks)
        if not dry_run:
            for task in tasks:
                task.delete()
            order.delete()
        return {
            "id": order_id,
            "result": "deleted",
            "reason": f"The Order and its unclaimed Task {task_ids} {'will be' if dry_run else 'were'} permanently deleted."
            if tasks else f"The Order has no Task and {'will be' if dry_run else 'was'} permanently deleted.",
        }


class AdminBulkDeleteTasksAPIView(AdminBulkDeleteAPIView):
    """Delete only the selected Tasks. The linked Order is kept but cancelled, never deleted."""

    model = Task

    def process_record(self, task, *, dry_run=False):
        reason = task_history_reason(task)
        if reason:
            return {"id": task.pk, "result": "skipped", "reason": reason}
        task_id = task.pk
        order = task.order
        if not dry_run:
            task.delete()
            if order is not None:
                # Without its Task the Order can no longer be fulfilled; mark it so
                # it does not look active. Its revenue snapshot is retained.
                order.fulfillment_status = Order.FulfillmentStatus.CANCELLED
                order.availability_status = Order.AvailabilityStatus.UNAVAILABLE
                order.save(update_fields=["fulfillment_status", "availability_status", "updated_at"])
                # Nothing was delivered: an API client gets its whole charge back, in this transaction.
                refund_api_order(order.pk)
        verb = "will be" if dry_run else "was"
        if order is not None:
            return {"id": task_id, "result": "deleted", "reason": f"The unclaimed Task {verb} deleted; Order #{order.pk} {verb} kept and marked cancelled."}
        return {"id": task_id, "result": "deleted", "reason": f"The unclaimed standalone Task {verb} permanently deleted."}


class AdminBulkDeleteServicesAPIView(AdminBulkDeleteAPIView):
    model = Service

    def process_record(self, service, *, dry_run=False):
        if service.orders.exists():
            if service.is_active and not dry_run:
                service.is_active = False
                service.save(update_fields=["is_active", "updated_at"])
            return {
                "id": service.pk,
                "result": "archived",
                "reason": (
                    "The Service is referenced by historical Orders; it will be disabled and retained."
                    if dry_run
                    else "The Service is referenced by historical Orders; it was disabled and retained."
                ),
            }
        service_id = service.pk
        if not dry_run:
            service.delete()
        return {"id": service_id, "result": "deleted", "reason": "The Service has no historical Orders and is eligible for permanent deletion."}
