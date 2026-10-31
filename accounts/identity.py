"""Identity rules that stop one person running several accounts.

Blocking rules (unique keys): one account per platform username and per payout
UPI ID. Flag-only rules: AccountSignal records which accounts share a sign-up
or login IP address or a browser device ID, as salted HMAC-SHA256 hashes only.
"""

import hashlib
import hmac
import uuid
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Count, Exists, F, OuterRef
from django.utils import timezone
from rest_framework.throttling import BaseThrottle

USERNAME_TAKEN_MESSAGE = "This username is already registered to another account."
DEVICE_COOKIE = "device_id"
LOGIN_IP_RETENTION = timedelta(days=180)


def normalize_username(value):
    """"  @Some.Name " -> "some.name": trimmed, lowercased, leading @ removed."""
    return (value or "").strip().lstrip("@").strip().lower() if isinstance(value, str) else ""


def normalize_platform(value):
    """" Instagram  " -> "instagram"."""
    return " ".join(value.split()).lower() if isinstance(value, str) else ""


def username_taken(username_key, *, platform_key=None, exclude_user_id=None):
    """Whether another account already owns this username.

    A primary platform username has no platform, so it is unique across every
    platform: it conflicts with any other account's primary username and with
    any other account's additional username of the same name. An additional
    username conflicts with another account's username on the same platform and
    with another account's primary username.
    """
    from .models import PlatformUsername, User

    if not username_key:
        return False
    primary = User.objects.filter(platform_username_key=username_key)
    additional = PlatformUsername.objects.filter(username_key=username_key)
    if platform_key is not None:
        additional = additional.filter(platform_key=platform_key)
    if exclude_user_id is not None:
        primary = primary.exclude(pk=exclude_user_id)
        additional = additional.exclude(user_id=exclude_user_id)
    return primary.exists() or additional.exists()


# ---------------------------------------------------------------------------
# Account signals (flag only)
# ---------------------------------------------------------------------------

def client_ip(request):
    """The client's address, honouring only the configured trusted proxies.

    Uses DRF's rule (REST_FRAMEWORK["NUM_PROXIES"], from DJANGO_NUM_PROXIES):
    with 0 proxies X-Forwarded-For is ignored, since any client can forge it.
    """
    return BaseThrottle().get_ident(request)


def signal_hash(kind, value):
    """HMAC-SHA256 of the value under the server-side salt; the raw value is never stored.

    ``kind`` is "ip" for both sign-up and login addresses, so the two match each other.
    """
    message = f"{kind}:{value}".encode("utf-8")
    return hmac.new(settings.ACCOUNT_SIGNAL_SALT.encode("utf-8"), message, hashlib.sha256).hexdigest()


def valid_device_id(value):
    try:
        return str(uuid.UUID(str(value))) == str(value).lower()
    except (TypeError, ValueError):
        return False


def record_signal(user, signal_type, kind, value, now=None):
    """Add or refresh one signal for the user (count, first/last seen)."""
    from .models import AccountSignal

    if not value:
        return
    now = now or timezone.now()
    value_hash = signal_hash(kind, value)
    updated = AccountSignal.objects.filter(user=user, signal_type=signal_type, value_hash=value_hash).update(
        count=F("count") + 1, last_seen=now,
    )
    if updated:
        return
    try:
        with transaction.atomic():
            AccountSignal.objects.create(user=user, signal_type=signal_type, value_hash=value_hash, first_seen=now, last_seen=now)
    except IntegrityError:
        # A concurrent request created it first.
        AccountSignal.objects.filter(user=user, signal_type=signal_type, value_hash=value_hash).update(
            count=F("count") + 1, last_seen=now,
        )


def record_request_signals(user, request, ip_signal_type):
    """Record the request's IP (as `signup_ip` or `login_ip`) and its device cookie."""
    from .models import AccountSignal

    record_signal(user, ip_signal_type, "ip", client_ip(request))
    record_signal(user, AccountSignal.SignalType.DEVICE_ID, "device", getattr(request, "device_id", None))


def shared_signal_hashes():
    """Signal hashes seen on more than one account."""
    from .models import AccountSignal

    return (AccountSignal.objects.order_by().values("value_hash")
            .annotate(accounts=Count("user", distinct=True)).filter(accounts__gt=1).values("value_hash"))


def has_linked_accounts():
    """Filter expression for users sharing any signal with another account."""
    from .models import AccountSignal

    return Exists(AccountSignal.objects.filter(user=OuterRef("pk"), value_hash__in=shared_signal_hashes()))


SIGNAL_KINDS = {"signup_ip": "IP address", "login_ip": "IP address", "device_id": "Device"}


def linked_accounts(user_ids):
    """{user_id: {other_user_id: {"IP address", "Device"}}} for the given users. Two queries."""
    from .models import AccountSignal

    own = list(AccountSignal.objects.filter(user_id__in=user_ids).values_list("user_id", "signal_type", "value_hash"))
    hashes = {value_hash for _user, _type, value_hash in own}
    holders = {}
    for other_id, signal_type, value_hash in AccountSignal.objects.filter(value_hash__in=hashes).values_list("user_id", "signal_type", "value_hash"):
        holders.setdefault(value_hash, []).append((other_id, SIGNAL_KINDS[signal_type]))
    links = {user_id: {} for user_id in user_ids}
    for user_id, signal_type, value_hash in own:
        for other_id, kind in holders.get(value_hash, []):
            if other_id != user_id:
                links[user_id].setdefault(other_id, set()).add(kind)
    return links


def purge_old_login_ips(now=None):
    """Delete login-IP signals not seen for 180 days. Returns how many were deleted."""
    from .models import AccountSignal

    cutoff = (now or timezone.now()) - LOGIN_IP_RETENTION
    deleted, _ = AccountSignal.objects.filter(signal_type=AccountSignal.SignalType.LOGIN_IP, last_seen__lt=cutoff).delete()
    return deleted
