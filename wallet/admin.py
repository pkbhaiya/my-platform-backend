from django.contrib import admin
from .models import Wallet, WalletTransaction, WithdrawalRequest
from django.core.exceptions import ValidationError
from django.contrib import messages
from .services import review_withdrawal_request


@admin.register(Wallet)
class WalletAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "balance",
        "created_at",
        "updated_at",
    )

    search_fields = (
        "user__email",
        "user__full_name",
    )

    readonly_fields = (
        "user",
        "balance",
        "created_at",
        "updated_at",
    )

    list_select_related = ("user",)
    ordering = ("-updated_at",)

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WalletTransaction)
class WalletTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "wallet",
        "transaction_type",
        "direction",
        "amount",
        "balance_after",
        "task",
        "created_at",
    )

    list_filter = (
        "transaction_type",
        "direction",
        "created_at",
    )

    search_fields = (
        "wallet__user__email",
        "description",
    )

    readonly_fields = (
        "wallet",
        "transaction_type",
        "direction",
        "amount",
        "balance_after",
        "task",
        "description",
        "created_at",
    )

    list_select_related = ("wallet", "wallet__user", "task")
    ordering = ("-created_at",)
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(WithdrawalRequest)
class WithdrawalRequestAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "amount",
        "payout_method",
        "payout_details",
        "status",
        "requested_at",
        "reviewed_by",
        "reviewed_at",
    )

    list_filter = (
        "status",
        "payout_method",
        "requested_at",
    )

    search_fields = (
        "user__email",
        "user__platform_username",
        "payout_details",
    )

    readonly_fields = (
        "user",
        "amount",
        "payout_method",
        "payout_details",
        "status",
        "reviewed_by",
        "review_note",
        "requested_at",
        "reviewed_at",
        "updated_at",
    )

    list_select_related = ("user", "reviewed_by")
    ordering = ("-requested_at",)
    list_per_page = 50

    actions = (
        "approve_selected_withdrawals",
        "reject_selected_withdrawals",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.action(description="Approve selected withdrawal requests")
    def approve_selected_withdrawals(self, request, queryset):
        success_count = 0

        for withdrawal in queryset:
            try:
                review_withdrawal_request(
                    withdrawal_id=withdrawal.pk,
                    reviewer=request.user,
                    action="approve",
                    review_note="Approved through Django Admin.",
                )
                success_count += 1
            except ValidationError as exc:
                self.message_user(
                    request,
                    f"Withdrawal #{withdrawal.pk}: {'; '.join(exc.messages)}",
                    level=messages.ERROR,
                )

        if success_count:
            self.message_user(
                request,
                f"Successfully approved {success_count} withdrawal request(s).",
                level=messages.SUCCESS,
            )

    @admin.action(description="Reject selected withdrawal requests")
    def reject_selected_withdrawals(self, request, queryset):
        success_count = 0

        for withdrawal in queryset:
            try:
                review_withdrawal_request(
                    withdrawal_id=withdrawal.pk,
                    reviewer=request.user,
                    action="reject",
                    review_note="Rejected through Django Admin.",
                )
                success_count += 1
            except ValidationError as exc:
                self.message_user(
                    request,
                    f"Withdrawal #{withdrawal.pk}: {'; '.join(exc.messages)}",
                    level=messages.ERROR,
                )

        if success_count:
            self.message_user(
                request,
                f"Successfully rejected {success_count} withdrawal request(s).",
                level=messages.SUCCESS,
            )
