"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path, re_path

from common.views import HealthCheckView
from orders.provider_api import ProviderAPIView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/health/", HealthCheckView.as_view(), name="health-check"),
    # SMM API v2 for reseller panels (Perfect Panel): POST /api/v2 or /api/v2/.
    re_path(r"^api/v2/?$", ProviderAPIView.as_view(), name="provider-api"),
    path("api/v1/auth/", include("accounts.urls")),
    path("api/v1/", include("common.urls")),
    path("api/v1/", include("services_catalog.urls")),
    path("api/v1/admin/", include("accounts.admin_urls")),
    path("api/v1/", include("tasks.urls")),
    path("api/v1/", include("wallet.urls")),
    path("api/v1/", include("orders.urls")),
    path("api/v1/", include("referrals.urls")),
]