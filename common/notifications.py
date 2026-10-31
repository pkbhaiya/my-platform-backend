"""Telegram alerts for administrators.

``send_admin_alert`` queues a message until the current transaction commits,
so a rolled-back order, review or withdrawal never announces itself. Delivery
has a 5-second timeout and swallows every error: an alert can never break the
action that triggered it. Messages carry IDs, counts and amounts only, never a
UPI ID, email, name, link or key. Each event can be switched off in Site
Configuration; without TELEGRAM_* settings every alert is skipped (and logged).
"""

import logging
import urllib.parse
import urllib.request
from datetime import timedelta, timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.utils import timezone

logger = logging.getLogger("common.notifications")

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT_SECONDS = 5
ORDER_WINDOW = timedelta(minutes=5)
ORDER_WINDOW_LIMIT = 10
HEARTBEAT_LIMIT = timedelta(minutes=10)
HEARTBEAT_CHECK_EVERY = 60  # seconds, per process

# Event -> SiteConfiguration toggle.
EVENT_TOGGLES = {
    "new_order": "alert_new_order",
    "task_paused": "alert_task_paused",
    "withdrawal_request": "alert_withdrawal_request",
    "reconciliation_mismatch": "alert_reconciliation_mismatch",
    "order_expired": "alert_order_expired",
    "cron_heartbeat": "alert_cron_heartbeat",
    "referral_flagged": "alert_referral_flagged",
    "referral_summary": "alert_referral_summary",
    "maintenance": "alert_maintenance",
    "api_low_balance": "alert_api_low_balance",
    "api_deposit": "alert_api_deposit",
    "api_insufficient_funds": "alert_api_insufficient_funds",
}


def rupees(value):
    return f"₹{Decimal(value).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def alerts_configured():
    return bool(settings.TELEGRAM_ALERTS_ENABLED and settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_ADMIN_CHAT_ID)


def event_enabled(event):
    from common.models import SiteConfiguration

    field = EVENT_TOGGLES[event]
    value = SiteConfiguration.objects.filter(pk=1).values_list(field, flat=True).first()
    return SiteConfiguration._meta.get_field(field).default if value is None else value


def deliver(event, text):
    """Send now. Returns True when Telegram accepted the message; never raises."""
    try:
        if not alerts_configured():
            logger.info("Telegram alert skipped (alerts are not configured): %s", event)
            return False
        if not event_enabled(event):
            logger.info("Telegram alert skipped (switched off in Site Configuration): %s", event)
            return False
    except Exception as exc:  # noqa: BLE001 - an alert must never break the caller
        logger.warning("Telegram alert %s failed: %s", event, type(exc).__name__)
        return False
    return post_message(event, text)


def post_message(label, text):
    """POST one message to the admin chat. Returns True when Telegram accepted it; never raises."""
    try:
        body = urllib.parse.urlencode({
            "chat_id": settings.TELEGRAM_ADMIN_CHAT_ID,
            "text": f"[TaskZippy] {text}",
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        request = urllib.request.Request(TELEGRAM_URL.format(token=settings.TELEGRAM_BOT_TOKEN), data=body, method="POST")
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
        if 200 <= status < 300:
            return True
        logger.warning("Telegram alert %s was refused with HTTP %s", label, status)
    except Exception as exc:  # noqa: BLE001 - an alert must never break the caller
        # Only the exception class: messages could include the request URL, which holds the token.
        logger.warning("Telegram alert %s failed: %s", label, type(exc).__name__)
    return False


def send_test_alert(admin):
    """Send a test message right away, whatever the event switches say. Returns (sent, detail)."""
    if not alerts_configured():
        return False, ("Telegram alerts are not configured on the server. Set TELEGRAM_ALERTS_ENABLED, "
                       "TELEGRAM_BOT_TOKEN and TELEGRAM_ADMIN_CHAT_ID, then restart the application.")
    when = f"{timezone.localtime():%d %b %Y %H:%M} IST"
    if post_message("test", f"Test message from the admin panel, sent by admin #{admin.pk} at {when}. Alerts are working."):
        return True, "Test message sent. Check the admin Telegram chat."
    return False, "Telegram did not accept the message. Check the bot token and chat ID; the server log has the error type."


def send_admin_alert(event, text):
    """Send ``text`` to the admin chat once the current transaction commits."""
    if event not in EVENT_TOGGLES:
        raise ValueError(f"Unknown alert event: {event}")
    transaction.on_commit(lambda: deliver(event, text))


# ---------------------------------------------------------------------------
# New orders: one message each, or one summary per busy 5-minute window
# ---------------------------------------------------------------------------

def window_start(moment):
    moment = moment.astimezone(dt_timezone.utc)
    return moment.replace(minute=moment.minute - moment.minute % 5, second=0, microsecond=0)


def notify_new_order(order):
    """Announce an Order after it commits; past 10 in one 5-minute window, batch them."""
    details = (order.pk, order.service.name, order.quantity, order.total_order_value, order.created_at)
    transaction.on_commit(lambda: _order_committed(*details))


def _order_committed(order_id, service_name, quantity, value, created_at):
    from common.models import OrderAlertWindow

    try:
        start = window_start(created_at)
        try:
            with transaction.atomic():
                OrderAlertWindow.objects.get_or_create(window_start=start)
        except IntegrityError:
            pass  # created by a concurrent order
        OrderAlertWindow.objects.filter(window_start=start).update(orders=F("orders") + 1)
        count = OrderAlertWindow.objects.values_list("orders", flat=True).get(window_start=start)
        if count <= ORDER_WINDOW_LIMIT:
            deliver("new_order", f"New order #{order_id}: {service_name}, quantity {quantity}, value {rupees(value)}.")
        flush_order_summaries()
    except Exception as exc:  # noqa: BLE001
        logger.warning("New-order alert bookkeeping failed: %s", type(exc).__name__)


def flush_order_summaries(now=None):
    """Send one summary for each finished window that had more than 10 orders. Returns how many."""
    from common.models import OrderAlertWindow
    from orders.models import Order

    now = now or timezone.now()
    sent = 0
    due = OrderAlertWindow.objects.filter(orders__gt=ORDER_WINDOW_LIMIT, summary_sent_at__isnull=True,
                                          window_start__lte=now - ORDER_WINDOW)
    for window in due:
        # A conditional update: exactly one process sends each summary.
        if not OrderAlertWindow.objects.filter(pk=window.pk, summary_sent_at__isnull=True).update(summary_sent_at=now):
            continue
        end = window.window_start + ORDER_WINDOW
        orders = Order.objects.filter(created_at__gte=window.window_start, created_at__lt=end)
        ids = list(orders.order_by("pk").values_list("pk", "total_order_value"))
        value = sum((amount for _pk, amount in ids), Decimal("0"))
        local = timezone.localtime(window.window_start)
        deliver("new_order", (
            f"{len(ids)} new orders between {local:%H:%M} and {timezone.localtime(end):%H:%M} "
            f"(#{ids[0][0]}–#{ids[-1][0]}), total value {rupees(value)}. "
            f"Only the first {ORDER_WINDOW_LIMIT} were announced one by one."
        ) if ids else f"{window.orders} new orders between {local:%H:%M} and {timezone.localtime(end):%H:%M}.")
        sent += 1
    # Old bookkeeping rows are of no further use.
    OrderAlertWindow.objects.filter(window_start__lt=now - timedelta(days=2)).filter(
        Q(summary_sent_at__isnull=False) | Q(orders__lte=ORDER_WINDOW_LIMIT)).delete()
    return sent


# ---------------------------------------------------------------------------
# Cron heartbeat
# ---------------------------------------------------------------------------

def record_cron_run(now=None):
    """Called by expire_claims: marks it alive and re-arms the missed-run alert."""
    from common.models import SiteConfiguration

    SiteConfiguration.load()
    SiteConfiguration.objects.filter(pk=1).update(expire_claims_last_run_at=now or timezone.now(), cron_alert_sent_at=None)


def check_cron_heartbeat(now=None):
    """Alert once when expire_claims has not run for 10 minutes. Returns True when it alerted."""
    from common.models import SiteConfiguration

    if not alerts_configured():
        return False
    now = now or timezone.now()
    last_run = SiteConfiguration.load().expire_claims_last_run_at
    if last_run and now - last_run < HEARTBEAT_LIMIT:
        return False
    stale = Q(expire_claims_last_run_at__isnull=True) | Q(expire_claims_last_run_at__lt=now - HEARTBEAT_LIMIT)
    if not SiteConfiguration.objects.filter(stale, pk=1, cron_alert_sent_at__isnull=True).update(cron_alert_sent_at=now):
        return False
    since = f"since {timezone.localtime(last_run):%d %b %Y %H:%M}" if last_run else "since it was set up"
    deliver("cron_heartbeat", f"The expire_claims job has not run {since}. Claims and old orders are not being expired; check the cron job.")
    return True


def maybe_check_cron_heartbeat():
    """Run the heartbeat check at most once a minute per process (called on API traffic)."""
    try:
        if alerts_configured() and cache.add("cron-heartbeat-check", 1, HEARTBEAT_CHECK_EVERY):
            check_cron_heartbeat()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cron heartbeat check failed: %s", type(exc).__name__)


class CronHeartbeatMiddleware:
    """Checks the expire_claims heartbeat on API traffic; never affects the response."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith("/api/"):
            maybe_check_cron_heartbeat()
        return response
