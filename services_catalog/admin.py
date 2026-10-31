from django.contrib import admin

from .models import Service


@admin.register(Service)
class ServiceAdmin(admin.ModelAdmin):
    list_display = (
        "service_id",
        "name",
        "price_per_1000",
        "minimum_order_quantity",
        "capacity_percentage",
        "eligible_level",
        "business_share_percentage",
        "user_reward_percentage",
        "is_active",
    )

    list_filter = (
        "is_active",
        "eligible_level",
        "order_source",
    )

    search_fields = (
        "name",
        "description",
    )

    readonly_fields = (
        "service_id",
        "business_share_percentage",
        "created_at",
        "updated_at",
    )

    fieldsets = (
        (
            "Service Information",
            {
                "fields": (
                    "service_id",
                    "name",
                    "description",
                    "target_type",
                    "is_active",
                )
            },
        ),
        (
            "Pricing and Capacity",
            {
                "fields": (
                    "price_per_1000",
                    "has_fulfillment_cost",
                    "fulfillment_cost_per_1000",
                    "minimum_order_quantity",
                    "capacity_percentage",
                )
            },
        ),
        (
            "Eligibility and Rewards",
            {
                "fields": (
                    "eligible_level",
                    "business_share_percentage",
                    "user_reward_percentage",
                )
            },
        ),
        (
            "Order Configuration",
            {
                "fields": (
                    "order_source",
                    "target_link_required",
                )
            },
        ),
        (
            "Timestamps",
            {
                "fields": (
                    "created_at",
                    "updated_at",
                )
            },
        ),
    )

    ordering = ("-created_at",)