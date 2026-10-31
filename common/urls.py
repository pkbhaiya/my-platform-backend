from django.urls import path

from .views import ApprovalInstructionsAPIView
from .admin_api_views import (
    AdminMaintenanceAPIView, AdminSettingsChangesAPIView, AdminSiteConfigurationAPIView, AdminTestAlertAPIView,
)
from .analytics_views import (
    AdminAnalyticsOrderDetailAPIView,
    AdminAnalyticsOrdersAPIView,
    AdminAnalyticsPayoutsAPIView,
    AdminAnalyticsSummaryAPIView,
    AdminAnalyticsUsersAPIView,
)
from .public_views import PublicConfigAPIView, PublicOverviewAPIView

urlpatterns = [
    path("admin/platform-settings/", AdminSiteConfigurationAPIView.as_view(), name="admin-platform-settings"),
    path("admin/platform-settings/changes/", AdminSettingsChangesAPIView.as_view(), name="admin-platform-settings-changes"),
    path("admin/platform-settings/test-alert/", AdminTestAlertAPIView.as_view(), name="admin-platform-settings-test-alert"),
    path("admin/maintenance/", AdminMaintenanceAPIView.as_view(), name="admin-maintenance"),
    path("admin/analytics/", AdminAnalyticsSummaryAPIView.as_view(), name="admin-analytics"),
    path("admin/analytics/orders/", AdminAnalyticsOrdersAPIView.as_view(), name="admin-analytics-orders"),
    path("admin/analytics/orders/<int:order_id>/", AdminAnalyticsOrderDetailAPIView.as_view(), name="admin-analytics-order"),
    path("admin/analytics/users/", AdminAnalyticsUsersAPIView.as_view(), name="admin-analytics-users"),
    path("admin/analytics/payouts/", AdminAnalyticsPayoutsAPIView.as_view(), name="admin-analytics-payouts"),
    path("public/overview/", PublicOverviewAPIView.as_view(), name="public-overview"),
    path("public/config/", PublicConfigAPIView.as_view(), name="public-config"),
    path(
        "approval-instructions/",
        ApprovalInstructionsAPIView.as_view(),
        name="approval-instructions",
    ),
]
