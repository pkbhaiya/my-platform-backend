from django.contrib import admin

from .models import Task, TaskClaim, TaskStrike, TaskUserClaim


@admin.register(Task)
class TaskAdmin(admin.ModelAdmin):
    """Read-only view of aggregate Tasks: one per Order with N claim slots."""

    list_display = (
        "id",
        "order",
        "title",
        "status",
        "total_slots",
        "remaining_slots",
        "reward_amount",
        "created_at",
    )

    list_filter = (
        "status",
        "created_at",
    )

    search_fields = (
        "title",
        "target_link",
        "order__id",
    )

    readonly_fields = [
        field.name for field in Task._meta.fields
    ]

    list_select_related = ("order",)

    ordering = ("-created_at",)
    list_per_page = 50

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TaskUserClaim)
class TaskUserClaimAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "user", "created_at")
    search_fields = ("user__email", "task__id")
    readonly_fields = [field.name for field in TaskUserClaim._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TaskStrike)
class TaskStrikeAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "task",
        "user",
        "issued_by",
        "reason",
        "created_at",
    )

    search_fields = (
        "user__email",
        "issued_by__email",
        "reason",
    )

    list_filter = ("created_at",)
    list_select_related = ("task", "user", "issued_by")
    ordering = ("-created_at",)
    list_per_page = 50

    readonly_fields = [
        field.name for field in TaskStrike._meta.fields
    ]

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TaskClaim)
class TaskClaimAdmin(admin.ModelAdmin):
    list_display = ("id", "task", "user", "status", "submitted_result", "claimed_at", "submitted_at", "reviewed_at")
    list_filter = ("status", "submitted_result", "claimed_at")
    search_fields = ("user__email", "user__platform_username", "task__id", "task__target_link")
    list_select_related = ("task", "user", "reviewed_by")
    readonly_fields = [field.name for field in TaskClaim._meta.fields]
    ordering = ("-created_at",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
