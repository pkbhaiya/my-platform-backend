"""Phase 9 Part A: one account per UPI ID and per platform username; linked-account flags."""

import importlib
from contextlib import redirect_stdout
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.apps import apps as django_apps
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.identity import client_ip, linked_accounts, purge_old_login_ips, record_signal, signal_hash
from accounts.models import AccountSignal, PlatformUsername, User
from common.models import SiteConfiguration
from wallet.models import PayoutUpiChange, Wallet, WithdrawalRequest
from wallet.services import create_withdrawal_request, set_payout_upi

UPI_URL = "/api/v1/wallet/payout-upi/"
TAKEN = "This username is already registered to another account."


def make_user(index, **extra):
    fields = dict(
        email=f"abuse-{index}@example.com", password="TestPassword!9317", full_name=f"Abuse {index}",
        platform_username=f"abuse_{index}", whatsapp_number=f"93456{index:05d}", state="DL",
        gender="female", approval_status="approved", is_active=True,
    )
    fields.update(extra)
    return User.objects.create_user(**fields)


def signup_payload(index, **extra):
    payload = {
        "full_name": f"New {index}", "email": f"new-{index}@example.com", "platform_username": f"new_{index}",
        "whatsapp_number": f"98111{index:05d}", "state": "DL", "gender": "female",
        "password": "Very-Strong-Pass-8812", "confirm_password": "Very-Strong-Pass-8812",
    }
    payload.update(extra)
    return payload


class PayoutUpiTests(TestCase):
    def setUp(self):
        self.user = make_user(1)
        self.other = make_user(2)
        self.api = APIClient()
        self.api.force_authenticate(self.user)

    def test_one_account_per_upi_id_whatever_the_case_or_spaces(self):
        set_payout_upi(user=self.other, upi_id="ravi@okbank")
        for variant in ("ravi@okbank", " Ravi@OkBank ", "RAVI@OKBANK"):
            response = self.api.put(UPI_URL, {"upi_id": variant}, format="json")
            self.assertEqual(response.status_code, 400, variant)
            self.assertEqual(response.data["upi_id"], ["This UPI ID is linked to another account."])
        # The column itself is unique too.
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=self.user.pk).update(upi_id="ravi@okbank")

    def test_first_save_then_one_change_per_30_days_all_logged(self):
        start = timezone.now()
        first = self.api.put(UPI_URL, {"upi_id": " Me@OkAxis "}, format="json")
        self.assertEqual((first.status_code, first.data["upi_id"]), (200, "me@okaxis"))
        self.assertIsNotNone(first.data["next_change_allowed_at"])
        # Saving the same value again is not a change.
        self.assertEqual(self.api.put(UPI_URL, {"upi_id": "me@okaxis"}, format="json").status_code, 200)
        blocked = self.api.put(UPI_URL, {"upi_id": "new@okaxis"}, format="json")
        self.assertEqual(blocked.status_code, 400)
        self.assertIn("once every 30 days", blocked.data["upi_id"][0])
        with self.assertRaises(ValidationError):
            set_payout_upi(user=self.user, upi_id="new@okaxis", now=start + timedelta(days=29, hours=23))
        set_payout_upi(user=self.user, upi_id="new@okaxis", now=start + timedelta(days=30, minutes=1))
        self.assertEqual(
            list(PayoutUpiChange.objects.filter(user=self.user).order_by("changed_at").values_list("previous_upi_id", "new_upi_id")),
            [(None, "me@okaxis"), ("me@okaxis", "new@okaxis")],
        )

    def test_withdrawals_always_use_the_saved_upi_id(self):
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"min_withdrawal_amount": Decimal("1.00")})
        Wallet.objects.create(user=self.user, balance=Decimal("10.00000000"))
        missing = self.api.post("/api/v1/wallet/withdrawals/", {"amount": "2"}, format="json")
        self.assertEqual(missing.data["detail"], ["Add your UPI ID before requesting a withdrawal."])
        set_payout_upi(user=self.user, upi_id="saved@okbank")
        response = self.api.post("/api/v1/wallet/withdrawals/", {"amount": "2", "payout_details": "someone.else@okbank"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(WithdrawalRequest.objects.get().payout_details, "saved@okbank")


class PlatformUsernameTests(TestCase):
    def setUp(self):
        self.owner = make_user(1, platform_username="Real.Name")
        self.api = APIClient()

    def test_signup_refuses_username_variants(self):
        for index, variant in enumerate(("real.name", "@Real.Name", "  @REAL.NAME  ")):
            response = self.api.post("/api/v1/auth/signup/", signup_payload(index, platform_username=variant), format="json")
            self.assertEqual(response.status_code, 400, variant)
            self.assertEqual(response.data["platform_username"], [TAKEN])
        self.assertEqual(self.api.post("/api/v1/auth/signup/", signup_payload(9, platform_username="@"), format="json").data["platform_username"], ["Enter your platform username."])
        created = self.api.post("/api/v1/auth/signup/", signup_payload(10, platform_username="@Someone.New"), format="json")
        self.assertEqual(created.status_code, 201)
        self.assertEqual(User.objects.get(email="new-10@example.com").platform_username_key, "someone.new")

    def test_the_key_is_unique_in_the_database(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            make_user(2, platform_username="@REAL.name")

    def test_additional_usernames_are_one_account_per_platform(self):
        worker = make_user(2)
        api = APIClient()
        api.force_authenticate(worker)
        url = "/api/v1/auth/me/platform-usernames/"
        PlatformUsername.objects.create(user=self.owner, platform_name="Instagram", username="insta.owner")
        taken = api.post(url, {"platform_name": " instagram ", "username": "@Insta.Owner"}, format="json")
        self.assertEqual((taken.status_code, taken.data["username"]), (400, [TAKEN]))
        primary = api.post(url, {"platform_name": "YouTube", "username": "real.name"}, format="json")
        self.assertEqual((primary.status_code, primary.data["username"]), (400, [TAKEN]))
        self.assertEqual(api.post(url, {"platform_name": "YouTube", "username": "insta.owner"}, format="json").status_code, 201)
        with self.assertRaises(IntegrityError), transaction.atomic():
            PlatformUsername.objects.create(user=worker, platform_name="INSTAGRAM", username="insta.owner")

    def test_a_legacy_duplicate_without_a_key_can_still_be_saved(self):
        legacy = make_user(3)
        User.objects.filter(pk=legacy.pk).update(platform_username="real.name", platform_username_key=None)
        legacy = User.objects.get(pk=legacy.pk)
        legacy.full_name = "Edited"
        legacy.save()
        self.assertIsNone(User.objects.get(pk=legacy.pk).platform_username_key)
        legacy.platform_username = "@real.NAME"
        with self.assertRaises(ValidationError):
            legacy.full_clean()


class AccountSignalTests(TestCase):
    def setUp(self):
        self.api = APIClient()

    def signup(self, index, **meta):
        return self.api.post("/api/v1/auth/signup/", signup_payload(index), format="json", **meta)

    def test_signup_and_login_store_only_salted_hashes(self):
        response = self.signup(1, REMOTE_ADDR="203.0.113.7")
        device = response.cookies["device_id"]
        self.assertTrue(device["httponly"])
        self.assertEqual((device["samesite"], device["path"]), ("Lax", "/api/v1/"))
        user = User.objects.get(email="new-1@example.com")
        User.objects.filter(pk=user.pk).update(approval_status="approved")
        self.api.post("/api/v1/auth/login/", {"email": "new-1@example.com", "password": "Very-Strong-Pass-8812"}, format="json", REMOTE_ADDR="203.0.113.7")
        signals = {signal.signal_type: signal for signal in AccountSignal.objects.filter(user=user)}
        self.assertEqual(set(signals), {"signup_ip", "login_ip", "device_id"})
        self.assertEqual(signals["signup_ip"].value_hash, signals["login_ip"].value_hash)
        self.assertEqual(signals["device_id"].value_hash, signal_hash("device", device.value))
        for signal in signals.values():
            self.assertEqual(len(signal.value_hash), 64)
            self.assertNotIn("203.0.113.7", signal.value_hash)
        with override_settings(ACCOUNT_SIGNAL_SALT="another-salt"):
            self.assertNotEqual(signal_hash("ip", "203.0.113.7"), signals["signup_ip"].value_hash)

    def test_the_device_cookie_is_reused_and_never_set_on_the_provider_api(self):
        first = self.signup(1)
        self.signup(2)  # the client sends the cookie it was given
        hashes = set(AccountSignal.objects.filter(signal_type="device_id").values_list("value_hash", flat=True))
        self.assertEqual(len(hashes), 1)
        self.assertNotIn("device_id", APIClient().post("/api/v2", {"action": "balance"}, format="json").cookies)
        self.assertIn("device_id", first.cookies)

    def test_shared_signals_link_accounts_for_admins_without_showing_values(self):
        one, two, three = make_user(1), make_user(2), make_user(3)
        record_signal(one, "device_id", "device", "11111111-1111-4111-8111-111111111111")
        record_signal(two, "device_id", "device", "11111111-1111-4111-8111-111111111111")
        record_signal(two, "signup_ip", "ip", "198.51.100.4")
        record_signal(three, "login_ip", "ip", "198.51.100.4")
        record_signal(three, "login_ip", "ip", "198.51.100.4")
        self.assertEqual(AccountSignal.objects.get(user=three).count, 2)
        self.assertEqual(linked_accounts([two.pk])[two.pk], {one.pk: {"Device"}, three.pk: {"IP address"}})

        admin = APIClient()
        admin.force_authenticate(make_user(0, is_staff=True))
        # The count moved from the list to the user detail view (Phase 11: lean list).
        counts = {user.pk: len(admin.get(f"/api/v1/admin/users/{user.pk}/").data["linked_accounts"]) for user in (one, two, three)}
        self.assertEqual((counts[one.pk], counts[two.pk], counts[three.pk]), (1, 2, 1))
        flagged = {row["id"] for row in admin.get("/api/v1/admin/users/", {"has_linked_accounts": "true"}).data["results"]}
        self.assertEqual(flagged, {one.pk, two.pk, three.pk})
        detail = admin.get(f"/api/v1/admin/users/{two.pk}/")
        self.assertEqual([(row["id"], row["shared"]) for row in detail.data["linked_accounts"]], [(one.pk, ["Device"]), (three.pk, ["IP address"])])
        self.assertNotIn("198.51.100.4", detail.content.decode())

    def test_proxy_headers_are_trusted_only_as_configured(self):
        request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.1", HTTP_X_FORWARDED_FOR="1.2.3.4, 203.0.113.9")
        with override_settings(REST_FRAMEWORK={"NUM_PROXIES": 0}):
            self.assertEqual(client_ip(request), "10.0.0.1")
        with override_settings(REST_FRAMEWORK={"NUM_PROXIES": 1}):
            self.assertEqual(client_ip(request), "203.0.113.9")

    def test_login_ips_are_kept_for_180_days(self):
        user = make_user(1)
        record_signal(user, "login_ip", "ip", "198.51.100.1")
        record_signal(user, "signup_ip", "ip", "198.51.100.2")
        AccountSignal.objects.update(last_seen=timezone.now() - timedelta(days=181))
        record_signal(user, "login_ip", "ip", "198.51.100.3")
        self.assertEqual(purge_old_login_ips(), 1)
        self.assertEqual(sorted(AccountSignal.objects.values_list("signal_type", flat=True)), ["login_ip", "signup_ip"])
        out = StringIO()
        call_command("purge_account_signals", stdout=out)
        self.assertIn("Deleted 0 login IP signal(s)", out.getvalue())


class IdentityBackfillTests(TestCase):
    def test_backfill_never_breaks_uniqueness(self):
        migration = importlib.import_module("accounts.migrations.0010_backfill_identity_keys")
        first, second, third = make_user(1), make_user(2), make_user(3)
        User.objects.filter(pk=second.pk).update(platform_username="@ABUSE_1")
        User.objects.update(platform_username_key=None)
        for user, upi in ((first, "Shared@okbank"), (second, "shared@OKBANK"), (third, "third@okbank")):
            WithdrawalRequest.objects.create(user=user, amount=Decimal("1"), payout_method="upi", payout_details=upi)
        with redirect_stdout(StringIO()):
            migration.backfill(django_apps, None)
        keys = dict(User.objects.values_list("pk", "platform_username_key"))
        self.assertEqual((keys[first.pk], keys[second.pk], keys[third.pk]), ("abuse_1", None, "abuse_3"))
        upis = dict(User.objects.values_list("pk", "upi_id"))
        self.assertEqual((upis[first.pk], upis[second.pk], upis[third.pk]), (None, None, "third@okbank"))
