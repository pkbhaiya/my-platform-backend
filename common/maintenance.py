"""Maintenance mode.

While it is on, ``MaintenanceModeMiddleware`` answers every user-panel API
request with 503 ``{"maintenance": true, "message", "expected_end"}``; staff
bypass it, and sign-in, the current user and public settings stay open. Sign-up
is refused, and /api/v2 refuses new orders unless the admin chose to keep
accepting them. Background jobs keep running, but claim deadlines stand still:
turning maintenance off gives every paused claim that time back.
"""

import logging
from datetime import timedelta

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError

from .models import MaintenanceEvent, SiteConfiguration
from .notifications import send_admin_alert

logger = logging.getLogger(__name__)

MAINTENANCE_API_ERROR = "Service temporarily unavailable"

# Answered for everyone during maintenance: signing in (and keeping the
# session), then, for reading only, the current user, health checks and the
# public settings.
SIGN_IN_PATHS = frozenset({"/api/v1/auth/login/", "/api/v1/auth/refresh/"})
READ_ONLY_PATHS = frozenset({"/api/v1/auth/me/", "/api/v1/health/", "/api/v1/approval-instructions/"})
READ_ONLY_PREFIXES = ("/api/v1/public/",)
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def active_maintenance():
    """The configuration while maintenance is on, otherwise None."""
    return (SiteConfiguration.objects.filter(pk=1, maintenance_enabled=True)
            .only("maintenance_message", "maintenance_expected_end").first())


def accepting_api_orders():
    """False only while maintenance is on with /api/v2 orders switched off."""
    return not SiteConfiguration.objects.filter(
        pk=1, maintenance_enabled=True, maintenance_accept_api_orders=False).exists()


def maintenance_body(config):
    return {"maintenance": True, "message": config.maintenance_message, "expected_end": config.maintenance_expected_end}


def maintenance_response(config):
    return JsonResponse(maintenance_body(config), status=503)


def is_open(request):
    path = request.path
    if path in SIGN_IN_PATHS:
        return True
    return request.method in SAFE_METHODS and (path in READ_ONLY_PATHS or path.startswith(READ_ONLY_PREFIXES))


def is_staff_request(request):
    """True for a staff session or a valid staff access token; never raises."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and (user.is_staff or user.is_superuser):
        return True
    authenticator = JWTAuthentication()
    try:
        header = authenticator.get_header(request)
        raw = authenticator.get_raw_token(header) if header else None
        if raw is None:
            return False
        user = authenticator.get_user(authenticator.get_validated_token(raw))
    except (InvalidToken, TokenError, AuthenticationFailed):
        return False
    return bool(user.is_active and (user.is_staff or user.is_superuser))


class MaintenanceModeMiddleware:
    """Refuses user-panel API requests while maintenance mode is on (staff bypass)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith("/api/v1/") and not is_open(request):
            config = active_maintenance()
            if config is not None and not is_staff_request(request):
                return maintenance_response(config)
        return self.get_response(request)


def _duration_text(seconds):
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if hours else f"{minutes} min"


def alert_text(event):
    when = f"{timezone.localtime(event.created_at):%d %b %Y %H:%M} IST"
    who = f"admin #{event.changed_by_id}" if event.changed_by_id else "the system"
    if event.enabled:
        return f"Maintenance mode turned ON by {who} at {when}."
    return (
        f"Maintenance mode turned OFF by {who} at {when}. It lasted {_duration_text(event.duration_seconds or 0)}; "
        f"{event.claims_extended} active claim(s) had their deadline extended by that time."
    )


def set_maintenance(*, enabled, by=None, now=None):
    """Turn maintenance mode on or off, log it and alert the admins.

    Returns the new MaintenanceEvent, or None when it was already in that state.
    """
    from tasks.services import extend_paused_claims

    now = now or timezone.now()
    with transaction.atomic():
        SiteConfiguration.load()
        config = SiteConfiguration.objects.select_for_update().get(pk=1)
        if config.maintenance_enabled == bool(enabled):
            return None
        changed_by = by if getattr(by, "is_authenticated", False) else None
        if enabled:
            config.maintenance_enabled, config.maintenance_started_at = True, now
            event = MaintenanceEvent.objects.create(enabled=True, changed_by=changed_by, created_at=now)
        else:
            started = config.maintenance_started_at or now
            event = MaintenanceEvent.objects.create(
                enabled=False, changed_by=changed_by, created_at=now,
                duration_seconds=int(max(now - started, timedelta(0)).total_seconds()),
                claims_extended=extend_paused_claims(started, now),
            )
            # The expected end belonged to this maintenance only.
            config.maintenance_enabled, config.maintenance_started_at, config.maintenance_expected_end = False, None, None
        config.save(update_fields=["maintenance_enabled", "maintenance_started_at", "maintenance_expected_end", "updated_at"])
        logger.info("Maintenance mode turned %s by user #%s", "on" if enabled else "off", event.changed_by_id or "-")
        send_admin_alert("maintenance", alert_text(event))
    return event


def maintenance_state():
    """Current maintenance settings and the latest changes, for the admin panel."""
    config = SiteConfiguration.load()
    events = MaintenanceEvent.objects.select_related("changed_by")[:10]
    return {
        "enabled": config.maintenance_enabled,
        "started_at": config.maintenance_started_at,
        "message": config.maintenance_message,
        "expected_end": config.maintenance_expected_end,
        "accept_api_orders": config.maintenance_accept_api_orders,
        "events": [{
            "id": event.id,
            "enabled": event.enabled,
            "changed_by": event.changed_by.email if event.changed_by_id else None,
            "created_at": event.created_at,
            "duration_seconds": event.duration_seconds,
            "claims_extended": event.claims_extended,
        } for event in events],
    }
