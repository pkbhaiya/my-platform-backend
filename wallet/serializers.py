from django.utils import timezone
from rest_framework import serializers

from common.models import SiteConfiguration

from .models import Wallet, WalletTransaction, WithdrawalRequest


class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = [
            "balance",
            "updated_at",
        ]
        read_only_fields = fields


class WalletTransactionSerializer(serializers.ModelSerializer):
    task_id = serializers.IntegerField(
        read_only=True,
        allow_null=True,
    )

    class Meta:
        model = WalletTransaction
        fields = [
            "id",
            "transaction_type",
            "direction",
            "amount",
            "balance_after",
            "task_id",
            "description",
            "created_at",
        ]
        read_only_fields = fields


class WithdrawalRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = WithdrawalRequest
        fields = [
            "id",
            "amount",
            "payout_method",
            "payout_details",
            "status",
            "review_note",
            "requested_at",
            "reviewed_at",
        ]
        read_only_fields = [
            "id",
            "payout_details",
            "status",
            "review_note",
            "requested_at",
            "reviewed_at",
        ]
        # Paid to the saved UPI ID (see set_payout_upi); any UPI sent here is ignored.
        extra_kwargs = {"payout_method": {"required": False, "default": WithdrawalRequest.PayoutMethod.UPI}}


class AdminWithdrawalRequestSerializer(WithdrawalRequestSerializer):
    """Admin-only view of a withdrawal with the requesting user's real identity.

    The identity fields are null when a legacy row has no resolvable user, so a
    caller can show a fallback instead of a guessed name.
    """

    currency = serializers.SerializerMethodField()
    upi_id = serializers.SerializerMethodField()
    requester_available = serializers.SerializerMethodField()
    user_id = serializers.SerializerMethodField()
    user_full_name = serializers.SerializerMethodField()
    user_platform_username = serializers.SerializerMethodField()
    user_email = serializers.SerializerMethodField()
    reviewed_by_email = serializers.SerializerMethodField()
    hold_until = serializers.SerializerMethodField()

    def get_hold_until(self, obj):
        """Set while a first withdrawal is still inside the first-withdrawal hold."""
        from .services import withdrawal_hold_until

        if "hold_hours" not in self.context:
            self.context["hold_hours"] = SiteConfiguration.objects.filter(pk=1).values_list(
                "first_withdrawal_hold_hours", flat=True).first() or 0
        hold_until = withdrawal_hold_until(obj, self.context["hold_hours"])
        return hold_until.isoformat() if hold_until and hold_until > timezone.now() else None

    @staticmethod
    def _requester(obj):
        if obj.user_id is None:
            return None
        try:
            return obj.user
        except obj._meta.get_field("user").related_model.DoesNotExist:
            return None

    def get_currency(self, obj):
        # Wallet amounts are stored and paid out in Indian rupees.
        return "INR"

    def get_upi_id(self, obj):
        return obj.payout_details if obj.payout_method == WithdrawalRequest.PayoutMethod.UPI else None

    def get_requester_available(self, obj):
        return self._requester(obj) is not None

    def get_user_id(self, obj):
        user = self._requester(obj)
        return user.pk if user else None

    def get_user_full_name(self, obj):
        user = self._requester(obj)
        return (user.full_name or None) if user else None

    def get_user_platform_username(self, obj):
        user = self._requester(obj)
        return (user.platform_username or None) if user else None

    def get_user_email(self, obj):
        user = self._requester(obj)
        return (user.email or None) if user else None

    def get_reviewed_by_email(self, obj):
        return obj.reviewed_by.email if obj.reviewed_by_id and obj.reviewed_by else None

    class Meta(WithdrawalRequestSerializer.Meta):
        fields = WithdrawalRequestSerializer.Meta.fields + [
            "currency", "upi_id", "requester_available", "user_id", "user_full_name",
            "user_platform_username", "user_email", "reviewed_by_email", "hold_until",
        ]
        read_only_fields = fields
