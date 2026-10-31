"""Test helpers: admins with a funded wallet and a key, as the API tests need them."""

from decimal import Decimal
from itertools import count

from django.contrib.auth import get_user_model

from .admin_wallets import ensure_admin_wallet, record_add_funds, regenerate_api_key

_made = [0]


def make_admin_wallet(name="Test admin", balance="100000.00", low_balance_threshold=None, superuser=False):
    """(wallet, key): a staff user whose wallet has a key and is topped up through the real services, so it reconciles."""
    _made[0] += 1
    slug = f"{name.lower().replace(' ', '-')}-{_made[0]}"
    User = get_user_model()
    admin = User.objects.create_user(
        email=f"{slug}@example.com", password="Admin-pass-123", full_name=name, platform_username=f"admin-{slug}",
        whatsapp_number=f"+9177{_made[0]:08d}", state="KA", gender="female", is_staff=True, is_superuser=superuser,
        approval_status="approved",
    )
    wallet = ensure_admin_wallet(admin)
    if low_balance_threshold is not None:
        type(wallet).objects.filter(pk=wallet.pk).update(low_balance_threshold=Decimal(low_balance_threshold))
    wallet, key = regenerate_api_key(wallet_id=wallet.pk)
    if Decimal(balance) > 0:
        record_add_funds(wallet_id=wallet.pk, amount=str(balance), reference="TEST-UTR", by=admin)
    wallet.refresh_from_db()
    return wallet, key


_link_numbers = count(1)


def unique_link(prefix="https://example.com/p"):
    """A different target link on every call: separate orders in a test never count as duplicates."""
    return f"{prefix}/{next(_link_numbers)}"
