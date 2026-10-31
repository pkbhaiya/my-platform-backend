from rest_framework import serializers


class IncomingOrderSerializer(serializers.Serializer):
    service_id = serializers.IntegerField(min_value=1)

    external_order_id = serializers.CharField(
        max_length=100,
        trim_whitespace=True,
    )

    target_link = serializers.CharField(
        max_length=2048,
        trim_whitespace=True,
        required=False,
        allow_blank=True,
        default="",
    )

    quantity = serializers.IntegerField(min_value=1)

    def validate_external_order_id(self, value):
        if not value:
            raise serializers.ValidationError(
                "External order ID is required."
            )

        return value

    def validate_quantity(self, value):
        if isinstance(value, bool) or value < 1:
            raise serializers.ValidationError(
                "Quantity must be a positive whole number."
            )

        return value
