from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers

from .models import Service


class AdminServiceSerializer(serializers.ModelSerializer):
    """Admin create/edit of a Service.

    ``business_share_percentage`` is read-only: any client value is ignored and
    Service.save() sets it to 100 - user_reward_percentage.
    """

    def validate_category(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("Enter a category, for example General.")
        return value

    def validate_eligible_level(self, value):
        if value != "all":
            from accounts.models import UserLevel
            if not UserLevel.objects.filter(code=value, is_active=True).exists():
                raise serializers.ValidationError("Choose an active configured user level.")
        return value

    class Meta:
        model = Service
        fields = [
            "service_id",
            "name",
            "description",
            "category",
            "target_type",
            "price_per_1000",
            "has_fulfillment_cost",
            "fulfillment_cost_per_1000",
            "minimum_order_quantity",
            "max_order_quantity",
            "capacity_percentage",
            "eligible_level",
            "business_share_percentage",
            "user_reward_percentage",
            "order_source",
            "target_link_required",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["service_id", "business_share_percentage", "created_at", "updated_at"]
        # Admin-set: the stable maximum published to reseller panels.
        extra_kwargs = {
            "max_order_quantity": {"required": True},
            # Blank reaches validate_category, which explains what to enter.
            "category": {"allow_blank": True},
        }

    def _save(self, service):
        # Service.save() runs full_clean(); report model errors as a 400.
        try:
            service.save()
        except DjangoValidationError as exc:
            raise serializers.ValidationError(
                exc.message_dict if hasattr(exc, "message_dict") else {"detail": exc.messages}
            )
        return service

    def create(self, validated_data):
        return self._save(Service(**validated_data))

    def update(self, instance, validated_data):
        for field, value in validated_data.items():
            setattr(instance, field, value)
        return self._save(instance)
