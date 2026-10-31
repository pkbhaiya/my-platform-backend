from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Sum
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from services_catalog.models import Service
from tasks.models import Task, TaskClaim

from .models import SiteConfiguration
from .serializers import telegram_url, whatsapp_url
from .throttles import PublicEndpointThrottle

RECENT_ACTIVITY_DAYS = 7
RECENT_ACTIVITY_LIMIT = 5
PUBLIC_CACHE_SECONDS = 60


def public_services():
    # The same catalog the reseller services API lists.
    return Service.objects.filter(
        is_active=True,
        order_source__in=(Service.OrderSource.API, Service.OrderSource.BOTH),
    ).order_by("service_id")


@method_decorator(cache_page(PUBLIC_CACHE_SECONDS), name="get")
class PublicOverviewAPIView(APIView):
    """
    Anonymous figures for the public landing page.

    GET /api/v1/public/overview/

    Only aggregates, approval times and the service catalog leave this view:
    no user, claim, order or Task identifiers, titles, target links or
    reward amounts. Rates are omitted when PUBLIC_SERVICE_RATES is off.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicEndpointThrottle]

    def get(self, request):
        now = timezone.now()
        open_tasks = Task.objects.filter(status=Task.Status.AVAILABLE, remaining_slots__gt=0).aggregate(
            count=Count("id"), slots=Sum("remaining_slots"),
        )
        services = list(public_services())
        show_rates = settings.PUBLIC_SERVICE_RATES
        recent_approvals = TaskClaim.objects.filter(
            status=TaskClaim.Status.APPROVED,
            reviewed_at__gte=now - timedelta(days=RECENT_ACTIVITY_DAYS),
            reviewed_at__lte=now,
        ).order_by("-reviewed_at").values_list("reviewed_at", flat=True)[:RECENT_ACTIVITY_LIMIT]

        return Response({
            "generated_at": now.isoformat(),
            "stats": {
                "open_tasks": open_tasks["count"],
                "open_slots": open_tasks["slots"] or 0,
                "approved_submissions": TaskClaim.objects.filter(status=TaskClaim.Status.APPROVED).count(),
                "approved_workers": User.objects.filter(
                    is_active=True,
                    is_staff=False,
                    is_superuser=False,
                    approval_status=User.ApprovalStatus.APPROVED,
                ).count(),
                "services": len(services),
            },
            # Minute precision only, so an approval cannot be matched to a person by its exact time.
            "recent_activity": [
                {"type": "submission_approved", "at": reviewed_at.replace(second=0, microsecond=0).isoformat()}
                for reviewed_at in recent_approvals
            ],
            "service_rates_public": show_rates,
            "services": [{
                "name": service.name,
                "description": service.description,
                "rate_per_1000": str(service.price_per_1000) if show_rates else None,
                "currency": "INR",
                "minimum_quantity": service.minimum_order_quantity,
            } for service in services],
        })


class PublicConfigAPIView(APIView):
    """
    Display settings the public pages need before anyone signs in.

    GET /api/v1/public/config/

    Only what the sign-up page and maintenance screen show: no limits,
    rates, keys or internal settings.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [PublicEndpointThrottle]

    def get(self, request):
        config = SiteConfiguration.load()
        return Response({
            "registration": {
                "open": config.registration_open,
                "closed_message": config.registration_closed_message,
                "allow_with_referral": config.registration_allow_with_referral,
            },
            "referral_enabled": config.referral_enabled,
            "whatsapp_url": whatsapp_url(config.whatsapp_contact),
            # The floating Telegram button: shown only with a valid link and the switch on.
            "telegram_url": telegram_url(config.telegram_url) or "",
            "telegram_button_enabled": config.telegram_button_enabled,
            "maintenance": {
                "enabled": config.maintenance_enabled,
                "message": config.maintenance_message if config.maintenance_enabled else "",
                "expected_end": config.maintenance_expected_end if config.maintenance_enabled else None,
            },
        })
