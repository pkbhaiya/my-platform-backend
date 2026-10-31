from django.core.cache import cache
from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from .maintenance import maintenance_state
from .models import SettingsChangeLog, SiteConfiguration
from .notifications import send_test_alert
from .serializers import AdminSiteConfigurationSerializer

TEST_ALERT_COOLDOWN = 10  # seconds between test messages per admin


class AdminSiteConfigurationAPIView(APIView):
    """GET every setting; PATCH any subset (one settings tab at a time)."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        return Response(AdminSiteConfigurationSerializer(SiteConfiguration.load()).data)

    @transaction.atomic
    def patch(self, request):
        SiteConfiguration.load()
        # Locked, so each change log row records the value it really replaced.
        configuration = SiteConfiguration.objects.select_for_update().get(pk=1)
        serializer = AdminSiteConfigurationSerializer(configuration, data=request.data, partial=True, context={"request": request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class SettingsChangePagination(PageNumberPagination):
    page_size = 10
    page_size_query_param = "page_size"
    max_page_size = 50


class AdminSettingsChangesAPIView(APIView):
    """GET /api/v1/admin/platform-settings/changes/?fields=a,b: who changed which setting, old -> new."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        changes = SettingsChangeLog.objects.select_related("changed_by")
        fields = [name for name in request.query_params.get("fields", "").split(",") if name]
        if fields:
            known = {field.name for field in SiteConfiguration._meta.get_fields()}
            unknown = sorted(set(fields) - known)
            if unknown:
                raise ValidationError({"fields": f"Unknown setting: {', '.join(unknown)}."})
            changes = changes.filter(field__in=fields)
        paginator = SettingsChangePagination()
        page = paginator.paginate_queryset(changes, request, view=self)
        return paginator.get_paginated_response([{
            "id": change.pk,
            "field": change.field,
            "old_value": change.old_value,
            "new_value": change.new_value,
            "changed_by": change.changed_by.email if change.changed_by_id else None,
            "changed_at": change.changed_at.isoformat(),
        } for change in page])


class AdminTestAlertAPIView(APIView):
    """POST: send a Telegram test message now (ignores the per-event switches)."""

    permission_classes = [IsAdminUser]

    def post(self, request):
        if not cache.add(f"telegram-test:{request.user.pk}", 1, TEST_ALERT_COOLDOWN):
            return Response({"sent": False, "detail": "Wait a few seconds before sending another test message."},
                            status=status.HTTP_429_TOO_MANY_REQUESTS)
        sent, detail = send_test_alert(request.user)
        return Response({"sent": sent, "detail": detail}, status=status.HTTP_200_OK if sent else status.HTTP_400_BAD_REQUEST)


class AdminMaintenanceAPIView(APIView):
    """GET the maintenance state and recent windows. Switch it with PATCH platform-settings."""

    permission_classes = [IsAdminUser]

    def get(self, request):
        return Response(maintenance_state())
