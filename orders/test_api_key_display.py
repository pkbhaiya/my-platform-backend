"""Full admin API keys: an encrypted copy, shown only to the wallet's owner and to superusers."""

import logging
from unittest.mock import patch
from urllib.parse import urlencode

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from common.test_notifications import CONFIGURED, sent_texts, telegram_ok
from orders.admin_wallets import ensure_admin_wallet
from orders.api_keys import hash_api_key
from orders.models import AdminWallet
from orders.testing import make_admin_wallet
from tasks.test_claim_lifecycle import make_user

MY_KEY = "/api/v1/admin/my-wallet/api-key/"
REGENERATE = "/api/v1/admin/my-wallet/regenerate-key/"
NO_STORE = "no-store, no-cache, must-revalidate, private"


def wallet_key_url(wallet):
    return f"/api/v1/admin/admin-wallets/{wallet.pk}/api-key/"


class CaptureEveryLog(logging.Handler):
    """Every record from every logger, at every level, while active."""

    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(self.format(record))

    def __enter__(self):
        loggers = [logging.getLogger()] + [
            logger for logger in logging.root.manager.loggerDict.values() if isinstance(logger, logging.Logger) and not logger.propagate
        ]
        self._saved = [(logger, logger.level) for logger in loggers]
        for logger in loggers:
            logger.addHandler(self)
            logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        for logger, level in self._saved:
            logger.removeHandler(self)
            logger.setLevel(level)


@override_settings(PROVIDER_API_REQUIRE_HTTPS=False)
class FullApiKeyTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.owner, self.owner_key = make_admin_wallet("Owner")
        self.other, self.other_key = make_admin_wallet("Other")
        self.boss, self.boss_key = make_admin_wallet("Boss", superuser=True)

    def client_for(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def provider(self, key, action="balance"):
        return APIClient().post("/api/v2", urlencode({"key": key, "action": action}),
                                content_type="application/x-www-form-urlencoded").json()

    def test_owner_sees_their_own_full_key_at_any_time(self):
        client = self.client_for(self.owner.admin)
        for url in (MY_KEY, wallet_key_url(self.owner), MY_KEY):
            response = client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response["Cache-Control"], NO_STORE)
            body = response.json()
            self.assertEqual((body["key"], body["configured"], body["wallet_id"]), (self.owner_key, True, self.owner.pk))
            self.assertIsNotNone(body["created_at"])
            self.assertNotIn("message", body)

    def test_another_non_superuser_admin_gets_403(self):
        response = self.client_for(self.other.admin).get(wallet_key_url(self.owner))
        self.assertEqual(response.status_code, 403)
        self.assertNotIn(self.owner_key, response.content.decode())
        # Their own key endpoint is theirs only.
        self.assertEqual(self.client_for(self.other.admin).get(MY_KEY).json()["key"], self.other_key)
        # Workers never get either endpoint.
        worker = self.client_for(make_user(1))
        self.assertEqual(worker.get(MY_KEY).status_code, 403)
        self.assertEqual(worker.get(wallet_key_url(self.owner)).status_code, 403)

    def test_superuser_sees_every_admins_key(self):
        boss = self.client_for(self.boss.admin)
        for wallet, key in ((self.owner, self.owner_key), (self.other, self.other_key), (self.boss, self.boss_key)):
            self.assertEqual(boss.get(wallet_key_url(wallet)).json()["key"], key)
        self.assertEqual(boss.get(wallet_key_url(AdminWallet(pk=999999))).status_code, 404)

    def test_the_key_is_never_in_wallet_rows_or_lists(self):
        boss, owner = self.client_for(self.boss.admin), self.client_for(self.owner.admin)
        responses = [
            boss.get("/api/v1/admin/admin-wallets/"),
            boss.get(f"/api/v1/admin/admin-wallets/{self.owner.pk}/"),
            boss.get(f"/api/v1/admin/admin-wallets/{self.owner.pk}/transactions/"),
            owner.get("/api/v1/admin/my-wallet/"),
            owner.get("/api/v1/admin/my-wallet/transactions/"),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 200)
            text = response.content.decode()
            for key in (self.owner_key, self.other_key, self.boss_key):
                self.assertNotIn(key, text)
            self.assertNotIn(AdminWallet.objects.get(pk=self.owner.pk).api_key_encrypted, text)

    def test_the_key_is_never_logged_or_sent_to_telegram(self):
        urlopen = patch("common.notifications.urllib.request.urlopen", return_value=telegram_ok()).start()
        self.addCleanup(patch.stopall)
        owner, other = self.client_for(self.owner.admin), self.client_for(self.other.admin)
        with CONFIGURED, self.captureOnCommitCallbacks(execute=True), CaptureEveryLog() as logs:
            owner.get(MY_KEY)
            other.get(wallet_key_url(self.owner))  # 403
            self.provider(self.owner_key)
            self.provider(self.owner_key, action="nope")
            new_key = owner.post(REGENERATE).json()["api_key"]
            self.provider(self.owner_key)  # the old key, now refused
            self.provider(new_key)
            AdminWallet.objects.filter(pk=self.other.pk).update(api_key_encrypted=Fernet(Fernet.generate_key()).encrypt(self.other_key.encode()).decode())
            self.client_for(self.boss.admin).get(wallet_key_url(self.other))  # unreadable copy: a warning
        self.assertTrue(any("provider_api" in line for line in logs.lines))  # the capture works
        self.assertTrue(any("could not be decrypted" in line for line in logs.lines))
        for key in (self.owner_key, new_key, self.other_key, self.boss_key):
            self.assertFalse([line for line in logs.lines if key in line])
            self.assertFalse([text for text in sent_texts(urlopen) if key in text])

    def test_regenerate_stores_the_hash_and_an_encrypted_copy(self):
        owner = self.client_for(self.owner.admin)
        response = owner.post(REGENERATE)
        self.assertEqual(response.status_code, 201)
        new_key = response.json()["api_key"]
        wallet = AdminWallet.objects.get(pk=self.owner.pk)
        self.assertEqual(wallet.api_key_hash, hash_api_key(new_key))
        self.assertTrue(wallet.api_key_encrypted)
        self.assertNotIn(new_key, wallet.api_key_encrypted)
        self.assertEqual(Fernet(settings.API_KEY_ENCRYPTION_KEY).decrypt(wallet.api_key_encrypted.encode()).decode(), new_key)
        self.assertEqual(owner.get(MY_KEY).json()["key"], new_key)
        self.assertEqual(self.provider(self.owner_key), {"error": "Invalid API key"})
        self.assertEqual(self.provider(new_key)["currency"], "INR")

    def test_a_key_made_before_encryption_shows_null_and_is_never_regenerated(self):
        AdminWallet.objects.filter(pk=self.owner.pk).update(api_key_encrypted="")
        before = AdminWallet.objects.get(pk=self.owner.pk).api_key_hash
        for client in (self.client_for(self.owner.admin), self.client_for(self.boss.admin)):
            body = client.get(wallet_key_url(self.owner)).json()
            self.assertEqual((body["key"], body["configured"], body["message"]), (None, True, "Regenerate to display the full key"))
        self.assertEqual(AdminWallet.objects.get(pk=self.owner.pk).api_key_hash, before)
        self.assertEqual(self.provider(self.owner_key)["currency"], "INR")

    def test_an_unreadable_copy_shows_null_and_logs_a_warning_without_the_key(self):
        other_fernet = Fernet(Fernet.generate_key())
        AdminWallet.objects.filter(pk=self.owner.pk).update(api_key_encrypted=other_fernet.encrypt(self.owner_key.encode()).decode())
        with self.assertLogs("orders.api_keys", "WARNING") as logs:
            body = self.client_for(self.owner.admin).get(MY_KEY).json()
        self.assertEqual((body["key"], body["message"]), (None, "Regenerate to display the full key"))
        self.assertNotIn(self.owner_key, "\n".join(logs.output))
        # A copy that is not this key's (it does not match the hash) is never shown either.
        AdminWallet.objects.filter(pk=self.owner.pk).update(api_key_encrypted=AdminWallet.objects.get(pk=self.other.pk).api_key_encrypted)
        with self.assertLogs("orders.api_keys", "WARNING"):
            self.assertIsNone(self.client_for(self.owner.admin).get(MY_KEY).json()["key"])

    def test_an_admin_without_a_key_gets_null_without_a_message(self):
        admin = get_user_model().objects.create_user(
            email="nokey@example.com", password="Admin-pass-123", full_name="No key", platform_username="nokey",
            whatsapp_number="+917700000099", state="KA", gender="female", is_staff=True,
        )
        ensure_admin_wallet(admin)
        body = self.client_for(admin).get(MY_KEY).json()
        self.assertEqual((body["key"], body["configured"], body["created_at"]), (None, False, None))
        self.assertNotIn("message", body)

    def test_revoking_a_sandbox_key_removes_its_encrypted_copy(self):
        AdminWallet.objects.filter(pk=self.other.pk).update(is_sandbox=True)
        response = self.client_for(self.boss.admin).post(f"/api/v1/admin/admin-wallets/{self.other.pk}/revoke-sandbox-key/")
        self.assertEqual(response.status_code, 200)
        wallet = AdminWallet.objects.get(pk=self.other.pk)
        self.assertEqual((wallet.api_key_hash, wallet.api_key_encrypted), (None, ""))
