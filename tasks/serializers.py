import math

from django.utils import timezone
from rest_framework import serializers

from .models import Task, TaskClaim
from .services import (
    claim_eligible_at,
    claim_expires_at,
    configured_claim_timeout_minutes,
    configured_target_wait_seconds,
    remaining_wait_seconds,
    submitted_result_label,
)


def task_description(task):
    return (task.order.service.description or "") if task.order_id else ""


class TaskSerializer(serializers.ModelSerializer):
    task_name = serializers.CharField(source="display_name", read_only=True)
    description = serializers.SerializerMethodField()
    target_type = serializers.CharField(read_only=True)

    def get_description(self, obj):
        return task_description(obj)

    status_display = serializers.CharField(
        source="get_status_display",
        read_only=True,
    )

    class Meta:
        model = Task
        fields = [
            "id",
            "title",
            "task_name",
            "description",
            "order",
            "unit_number",
            "total_slots",
            "remaining_slots",
            "target_link",
            "target_type",
            "reward_amount",
            "status",
            "status_display",
            "created_at",
        ]
        read_only_fields = fields


class AvailableTaskSerializer(TaskSerializer):
    """Available Tasks never include the target link; it is returned only for the user's own claim."""

    class Meta(TaskSerializer.Meta):
        fields = [field for field in TaskSerializer.Meta.fields if field != "target_link"]
        read_only_fields = fields


class TaskSubmissionSerializer(serializers.Serializer):
    result = serializers.ChoiceField(choices=TaskClaim.Result.choices)
    note = serializers.CharField(
        required=False,
        allow_blank=True,
        max_length=2000,
        default="",
    )


class TaskClaimSerializer(serializers.ModelSerializer):
    id = serializers.IntegerField(source="task_id", read_only=True)
    claim_id = serializers.IntegerField(source="pk", read_only=True)
    title = serializers.CharField(source="task.title", read_only=True)
    task_name = serializers.CharField(source="task.display_name", read_only=True)
    description = serializers.SerializerMethodField()
    order = serializers.IntegerField(source="task.order_id", read_only=True)
    unit_number = serializers.IntegerField(source="task.unit_number", read_only=True)
    target_link = serializers.CharField(source="task.target_link", read_only=True)
    target_type = serializers.CharField(source="task.target_type", read_only=True)
    reward_amount = serializers.DecimalField(source="task.reward_amount", max_digits=18, decimal_places=8, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    total_slots = serializers.IntegerField(source="task.total_slots", read_only=True)
    remaining_slots = serializers.IntegerField(source="task.remaining_slots", read_only=True)
    target_opened_at = serializers.DateTimeField(read_only=True)
    reviewed_at = serializers.DateTimeField(read_only=True)
    submitted_result_display = serializers.SerializerMethodField()
    submission_eligible = serializers.SerializerMethodField()
    eligible_at = serializers.SerializerMethodField()
    server_time = serializers.SerializerMethodField()
    wait_seconds = serializers.SerializerMethodField()
    remaining_wait_seconds = serializers.SerializerMethodField()
    # Claim time limit: set while the claim is still `claimed`, else null.
    expires_at = serializers.SerializerMethodField()
    expires_in_seconds = serializers.SerializerMethodField()
    claim_timeout_minutes = serializers.SerializerMethodField()

    def _now(self):
        # One server instant per response, so every timer field agrees.
        if "_now" not in self.context:
            self.context["_now"] = timezone.now()
        return self.context["_now"]

    def _wait_seconds(self, obj):
        # Same rule as services.claim_wait_seconds, but unopened and older claims
        # read the configured wait once per response instead of once per field.
        if obj.target_wait_seconds:
            return obj.target_wait_seconds
        if "_configured_wait" not in self.context:
            self.context["_configured_wait"] = configured_target_wait_seconds()
        return self.context["_configured_wait"]

    def _timeout_minutes(self):
        if "_claim_timeout" not in self.context:
            self.context["_claim_timeout"] = configured_claim_timeout_minutes()
        return self.context["_claim_timeout"]

    def get_claim_timeout_minutes(self, obj):
        return self._timeout_minutes()

    def get_expires_at(self, obj):
        expires_at = claim_expires_at(obj, self._timeout_minutes())
        return expires_at.isoformat() if expires_at else None

    def get_expires_in_seconds(self, obj):
        expires_at = claim_expires_at(obj, self._timeout_minutes())
        return max(0, math.ceil((expires_at - self._now()).total_seconds())) if expires_at else None

    def get_submitted_result_display(self, obj):
        return submitted_result_label(obj)

    def get_wait_seconds(self, obj):
        return self._wait_seconds(obj)

    def get_eligible_at(self, obj):
        eligible_at = claim_eligible_at(obj, self._wait_seconds(obj))
        return eligible_at.isoformat() if eligible_at else None

    def get_server_time(self, obj):
        return self._now().isoformat()

    def get_remaining_wait_seconds(self, obj):
        return remaining_wait_seconds(obj, self._now(), self._wait_seconds(obj))

    def get_submission_eligible(self, obj):
        eligible_at = claim_eligible_at(obj, self._wait_seconds(obj))
        return bool(eligible_at and self._now() >= eligible_at)

    def get_description(self, obj):
        return task_description(obj.task)

    class Meta:
        model = TaskClaim
        fields = [
            "id", "claim_id", "title", "task_name", "description", "order", "unit_number",
            "target_link", "target_type", "reward_amount", "status", "status_display",
            "total_slots", "remaining_slots", "claimed_at", "submitted_at",
            "submitted_result", "submission_note", "target_opened_at", "submission_eligible",
            "eligible_at", "server_time", "wait_seconds", "remaining_wait_seconds",
            "reviewed_at", "submitted_result_display",
            "expires_at", "expires_in_seconds", "claim_timeout_minutes",
        ]
