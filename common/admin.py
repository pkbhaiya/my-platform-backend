from django.contrib import admin

from .models import SiteConfiguration


@admin.register(SiteConfiguration)
class SiteConfigurationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "company_username",
        "whatsapp_contact",
        "updated_at",
    )

    # Read-only here: settings are edited in the admin panel (Platform Settings),
    # which validates every range and records each change in SettingsChangeLog.
    readonly_fields = ("updated_at",)

    def has_change_permission(self, request, obj=None):
        return False

    def has_add_permission(self, request):
        # Allow only one site configuration record.
        return not SiteConfiguration.objects.filter(pk=1).exists()

    def has_delete_permission(self, request, obj=None):
        # Prevent accidental deletion of site configuration.
        return False

    def get_queryset(self, request):
        # Display only the singleton configuration record.
        return super().get_queryset(request).filter(pk=1)
