from django.contrib import admin

from .models import AdminWallet, AdminWalletTransaction, Order


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "service",
        "source",
        "admin_wallet",
        "external_order_id",
        "quantity",
        "total_order_value",
        "charge_amount",
        "availability_status",
        "fulfillment_status",
        "created_at",
    )

    list_filter = (
        "source",
        "availability_status",
        "fulfillment_status",
        "created_at",
    )

    search_fields = (
        "external_order_id",
        "service__name",
        "target_link",
    )

    readonly_fields = (
        "service",
        "source",
        "admin_wallet",
        "charge_amount",
        "external_order_id",
        "idempotency_key",
        "target_link",
        "normalized_link",
        "quantity",
        "price_per_1000_snapshot",
        "fulfillment_cost_per_1000_snapshot",
        "has_fulfillment_cost_snapshot",
        "business_share_percentage_snapshot",
        "user_reward_percentage_snapshot",
        "capacity_percentage_snapshot",
        "eligible_user_count_at_acceptance",
        "capacity_limit_at_acceptance",
        "total_order_value",
        "availability_status",
        "fulfillment_status",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        # Orders must pass through validated order-processing logic.
        return False

    def has_delete_permission(self, request, obj=None):
        # Preserve order records for financial auditing.
        return False

class ReadOnlyAdmin(admin.ModelAdmin):
    """Balances move only through orders.admin_wallets (locks + ledger); the Admin Panel's My Wallet page does that."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AdminWallet)
class AdminWalletAdmin(ReadOnlyAdmin):
    list_display = ("id", "admin", "api_key_prefix", "balance", "low_balance_threshold", "api_key_last_used_at", "created_at")
    search_fields = ("admin__email", "api_key_prefix")
    exclude = ("api_key_hash",)


@admin.register(AdminWalletTransaction)
class AdminWalletTransactionAdmin(ReadOnlyAdmin):
    list_display = ("id", "wallet", "transaction_type", "direction", "amount", "balance_after", "order", "reference", "created_at")
    list_filter = ("transaction_type", "direction")
    search_fields = ("reference", "note")
    raw_id_fields = ("order",)
