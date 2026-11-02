"""Create or update the sandbox account an outside panel's support team tests /api/v2 with.

A staff account with an unusable password (it can never sign in to the admin
panel; it exists only for its API key) whose admin wallet is a sandbox wallet
(orders.sandbox), funded once with ₹100.00. A key is generated, and printed
once, when the wallet has none or with --new-key. Safe to run again.
"""

import uuid
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction

from orders.admin_wallets import ensure_admin_wallet, record_add_funds, regenerate_api_key, update_low_balance_threshold
from orders.models import AdminWallet

FUNDS = Decimal("100.00")
FUNDS_REFERENCE = "PP support test"
# A reserved domain (RFC 2606): no mail can ever be delivered to this account.
EMAIL_DOMAIN = "pythonsmm.invalid"


class Command(BaseCommand):
    help = "Create or update the API-only sandbox account (default perfectpanel-test) and print a new key once."

    def add_arguments(self, parser):
        parser.add_argument("--name", default="perfectpanel-test", help="Account name (platform username and email local part).")
        parser.add_argument("--new-key", action="store_true", help="Replace the wallet's key even if it has one.")

    def handle(self, *args, name, new_key, **options):
        User = get_user_model()
        email = f"{name}@{EMAIL_DOMAIN}"
        with transaction.atomic():
            user = User.objects.filter(email=email).first()
            if user is None:
                user = User(email=email, full_name=name, platform_username=name, whatsapp_number="", gender="",
                            approval_status=User.ApprovalStatus.APPROVED)
            # Staff, so its key is accepted; never a superuser; never able to sign in.
            user.is_staff, user.is_superuser, user.is_active = True, False, True
            user.set_unusable_password()
            user.save()
            wallet = ensure_admin_wallet(user)
            AdminWallet.objects.filter(pk=wallet.pk).update(is_sandbox=True)
            # A test balance: no low-balance alerts.
            update_low_balance_threshold(wallet_id=wallet.pk, low_balance_threshold="0.00")
            # One funding per wallet, however often this runs (the request key is fixed).
            _, funded = record_add_funds(
                wallet_id=wallet.pk, amount=str(FUNDS), reference=FUNDS_REFERENCE,
                note="Sandbox balance for API tests", by=None,
                request_key=uuid.uuid5(uuid.NAMESPACE_URL, f"taskzippy:sandbox-funds:{wallet.pk}"),
            )
            wallet.refresh_from_db()
            key = None
            if new_key or not wallet.api_key_hash:
                wallet, key = regenerate_api_key(wallet_id=wallet.pk)

        self.stdout.write(f"Sandbox account #{user.pk} ({email}), admin wallet #{wallet.pk}: "
                          f"balance ₹{wallet.balance}{' (₹' + str(FUNDS) + ' added now)' if funded else ''}.")
        if key:
            self.stdout.write(f"API key (shown once, not stored): {key}")
        else:
            self.stdout.write(f"The wallet keeps its key ({wallet.api_key_prefix}…); pass --new-key to replace it.")
