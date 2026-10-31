"""Admin wallet API keys: generated here, shown once, stored only as a hash."""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.utils import timezone

from .models import AdminWallet

KEY_PREFIX_LENGTH = 8
# Keys are 43 characters; anything far longer is not a key and is not hashed.
MAX_KEY_LENGTH = 256
# api_key_last_used_at is written at most this often per wallet, not on every request.
LAST_USED_RESOLUTION = timedelta(minutes=1)


def hash_api_key(key):
    # The key is 256 random bits, so a fast digest is enough: there is nothing to guess.
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def new_api_key():
    """A new key and the (prefix, hash) to store. The plaintext is never saved."""
    key = secrets.token_urlsafe(32)
    return key, key[:KEY_PREFIX_LENGTH], hash_api_key(key)


def owner_may_use_api(admin):
    """Only an active staff account's key is accepted."""
    return bool(admin.is_active and admin.is_staff)


def authenticate_admin_wallet(provided, *, now=None):
    """The AdminWallet whose key is `provided` and whose owner is active staff, or None.

    The row is found by the key's SHA-256 digest (a unique column), then the
    digests are compared in constant time. A key whose owner is inactive or no
    longer staff is refused like an unknown one.
    """
    if not isinstance(provided, str) or not provided or len(provided) > MAX_KEY_LENGTH:
        return None
    digest = hash_api_key(provided)
    wallet = AdminWallet.objects.select_related("admin").filter(api_key_hash=digest).first()
    if wallet is None or not hmac.compare_digest(digest, wallet.api_key_hash) or not owner_may_use_api(wallet.admin):
        return None
    now = now or timezone.now()
    if wallet.api_key_last_used_at is None or now - wallet.api_key_last_used_at >= LAST_USED_RESOLUTION:
        AdminWallet.objects.filter(pk=wallet.pk).update(api_key_last_used_at=now)
        wallet.api_key_last_used_at = now
    return wallet
