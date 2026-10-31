from django.urls import path

from .admin_api_views import AdminServiceDetailAPIView, AdminServiceListCreateAPIView
from common.admin_bulk_delete import AdminBulkDeleteServicesAPIView

urlpatterns = [
    path("admin/services/", AdminServiceListCreateAPIView.as_view(), name="admin-services"),
    path("admin/services/bulk-delete/", AdminBulkDeleteServicesAPIView.as_view(), name="admin-services-bulk-delete"),
    path("admin/services/<int:service_id>/", AdminServiceDetailAPIView.as_view(), name="admin-service-detail"),
]
