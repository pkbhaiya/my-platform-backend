"""Phase 10-FIX Step 4: Platform Settings: validation per field, per-tab saves, change log, test alert, withdrawal limits."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User
from accounts.test_registration_control import payload as signup_payload
from common.models import SettingsChangeLog, SiteConfiguration
from common.test_notifications import CONFIGURED, sent_texts, telegram_ok
from tasks.test_claim_lifecycle import make_user
from wallet.models import Wallet, WithdrawalRequest
from wallet.services import create_withdrawal_request, review_withdrawal_request

SETTINGS = "/api/v1/admin/platform-settings/"
CHANGES = "/api/v1/admin/platform-settings/changes/"
TEST_ALERT = "/api/v1/admin/platform-settings/test-alert/"

# field -> (a valid value, invalid values)
FIELD_CASES = {
    "company_profile_image_url": ("https://example.com/logo.png", ["ftp://example.com/logo.png", "not a url", "https://" + "a" * 500]),
    "company_username": ("@official", ["x" * 151]),
    "whatsapp_contact": ("+91 98765 43210", ["98765 43210", "+91 abc"]),
    "approval_title": ("Waiting for approval", ["", "   ", "x" * 201]),
    "approval_instructions": ("Follow us, then message us.", ["x" * 5001]),
    "approval_instructions_hi": ("हमें फॉलो करें, फिर व्हाट्सएप पर संदेश भेजें।", ["x" * 5001]),
    "disabled_account_message": ("Disabled.", ["", "x" * 2001]),
    "registration_open": (False, ["maybe"]),
    "registration_closed_message": ("Closed.", ["", "x" * 2001]),
    "registration_allow_with_referral": (True, ["maybe"]),
    "maintenance_message": ("Back soon.", [" ", "x" * 2001]),
    "maintenance_accept_api_orders": (False, ["maybe"]),
    "target_wait_seconds": (20, [0, 3601, "ten", 1.5]),
    "claim_timeout_minutes": (45, [4, 1441, "x"]),
    "tna_pause_min_reports": (5, [0, 1001]),
    "tna_pause_percent": (50, [0, 101]),
    "order_max_age_hours": (48, [0, 2161]),
    "min_withdrawal_amount": ("50.00", ["0", "-1", "1.001", "abc"]),
    "withdrawal_max_per_day": ("500.00", ["-1", "1.001"]),
    "first_withdrawal_hold_hours": (24, [-1, 721, "x"]),
    "referral_enabled": (True, ["maybe"]),
    "referral_required_approved_tasks": (5, [0, 1001]),
    "referral_bonus_amount": ("15.00", ["0", "0.001"]),
    "referral_qualify_within_days": (30, [-1, 3651]),
    "referral_monthly_bonus_cap": (3, [-1, 10001]),
    "alert_new_order": (False, ["maybe"]),
    "alert_maintenance": (False, ["maybe"]),
    "alert_api_low_balance": (False, ["maybe"]),
    "telegram_url": ("https://t.me/taskzippy", ["http://t.me/x", "https://example.com/x"]),
    "telegram_button_enabled": (False, ["maybe"]),
}


class SettingsValidationTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.admin = make_user(1, is_staff=True)
        self.api.force_authenticate(self.admin)

    def test_every_field_accepts_valid_and_refuses_invalid_values(self):
        for field, (valid, invalid_values) in FIELD_CASES.items():
            for invalid in invalid_values:
                with self.subTest(field=field, value=invalid):
                    response = self.api.patch(SETTINGS, {field: invalid}, format="json")
                    self.assertEqual(response.status_code, 400, response.data)
                    self.assertIn(field, response.data)
            with self.subTest(field=field, value=valid):
                response = self.api.patch(SETTINGS, {field: valid}, format="json")
                self.assertEqual(response.status_code, 200, response.data)

    def test_cross_field_rules(self):
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"min_withdrawal_amount": Decimal("100.00")})
        response = self.api.patch(SETTINGS, {"withdrawal_max_per_day": "50.00"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("withdrawal_max_per_day", response.data)
        self.assertEqual(self.api.patch(SETTINGS, {"withdrawal_max_per_day": "0"}, format="json").status_code, 200)
        response = self.api.patch(SETTINGS, {"claim_timeout_minutes": 5, "target_wait_seconds": 300}, format="json")
        self.assertIn("claim_timeout_minutes", response.data)

    def test_one_tab_saves_only_its_fields(self):
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"company_username": "@keep", "claim_timeout_minutes": 30})
        response = self.api.patch(SETTINGS, {"claim_timeout_minutes": 40, "order_max_age_hours": 24}, format="json")
        self.assertEqual(response.status_code, 200)
        config = SiteConfiguration.load()
        self.assertEqual((config.claim_timeout_minutes, config.order_max_age_hours, config.company_username), (40, 24, "@keep"))

    def test_only_admins(self):
        worker = APIClient()
        worker.force_authenticate(make_user(2))
        self.assertEqual(worker.get(SETTINGS).status_code, 403)
        self.assertEqual(worker.patch(SETTINGS, {"registration_open": False}, format="json").status_code, 403)
        self.assertEqual(worker.get(CHANGES).status_code, 403)
        self.assertEqual(worker.post(TEST_ALERT).status_code, 403)

    def test_changes_apply_on_the_next_request(self):
        self.assertEqual(self.api.patch(SETTINGS, {"registration_open": False}, format="json").status_code, 200)
        self.assertEqual(APIClient().post("/api/v1/auth/signup/", signup_payload(1), format="json").status_code, 403)
        self.assertEqual(self.api.patch(SETTINGS, {"registration_open": True}, format="json").status_code, 200)
        self.assertEqual(APIClient().post("/api/v1/auth/signup/", signup_payload(2), format="json").status_code, 201)

    def test_django_admin_shows_settings_read_only(self):
        superuser = User.objects.create_superuser(
            email="root@example.com", password="Root-Password-8812", full_name="Root", platform_username="root",
            whatsapp_number="9000000001", state="DL", gender="female")
        SiteConfiguration.load()
        self.client.force_login(superuser)
        url = reverse("admin:common_siteconfiguration_change", args=[1])
        self.assertEqual(self.client.get(url).status_code, 200)
        self.client.post(url, {"company_username": "@hijack"})
        self.assertNotEqual(SiteConfiguration.load().company_username, "@hijack")


class HindiInstructionsTests(TestCase):
    def test_saved_trimmed_logged_and_sent_with_the_support_details(self):
        admin = APIClient()
        admin.force_authenticate(make_user(1, is_staff=True))
        text = "1. हमारे इंस्टाग्राम अकाउंट को फॉलो करें।\n2. व्हाट्सएप पर अपना ईमेल भेजें।"
        response = admin.patch(SETTINGS, {"approval_instructions_hi": f"  {text}  "}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SiteConfiguration.load().approval_instructions_hi, text)
        change = SettingsChangeLog.objects.get(field="approval_instructions_hi")
        self.assertEqual((change.old_value, change.new_value), ("", text))
        # The Pending Approval page reads it from the account-status support details.
        pending = APIClient()
        pending.force_authenticate(make_user(2, approval_status="pending"))
        self.assertEqual(pending.get("/api/v1/auth/me/account-status/").data["support"]["approval_instructions_hi"], text)
        self.assertEqual(APIClient().get("/api/v1/approval-instructions/").data["approval_instructions_hi"], text)
        # Optional: clearing it is allowed (the page then shows English only).
        self.assertEqual(admin.patch(SETTINGS, {"approval_instructions_hi": ""}, format="json").status_code, 200)
        self.assertEqual(SiteConfiguration.load().approval_instructions_hi, "")


class SettingsChangeLogTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.admin = make_user(1, is_staff=True)
        self.api.force_authenticate(self.admin)
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"claim_timeout_minutes": 30, "referral_bonus_amount": Decimal("10.00")})

    def test_each_changed_field_is_logged_with_who_and_old_and_new(self):
        self.api.patch(SETTINGS, {"claim_timeout_minutes": 45, "referral_bonus_amount": "10.00", "registration_open": False}, format="json")
        rows = {row.field: row for row in SettingsChangeLog.objects.all()}
        self.assertEqual(set(rows), {"claim_timeout_minutes", "registration_open"})  # the unchanged bonus is not logged
        self.assertEqual((rows["claim_timeout_minutes"].old_value, rows["claim_timeout_minutes"].new_value), ("30", "45"))
        self.assertEqual((rows["registration_open"].old_value, rows["registration_open"].new_value), ("true", "false"))
        self.assertEqual(rows["registration_open"].changed_by, self.admin)

    def test_refused_saves_log_nothing(self):
        self.api.patch(SETTINGS, {"claim_timeout_minutes": 45, "tna_pause_percent": 500}, format="json")
        self.assertFalse(SettingsChangeLog.objects.exists())
        self.assertEqual(SiteConfiguration.load().claim_timeout_minutes, 30)

    def test_history_endpoint_filters_by_field(self):
        self.api.patch(SETTINGS, {"claim_timeout_minutes": 45}, format="json")
        self.api.patch(SETTINGS, {"referral_bonus_amount": "12.50"}, format="json")
        data = self.api.get(f"{CHANGES}?fields=referral_bonus_amount,referral_enabled").data
        self.assertEqual(data["count"], 1)
        row = data["results"][0]
        self.assertEqual((row["field"], row["old_value"], row["new_value"], row["changed_by"]), ("referral_bonus_amount", "10.00", "12.50", self.admin.email))
        self.assertEqual(self.api.get(CHANGES).data["count"], 2)
        self.assertEqual(self.api.get(f"{CHANGES}?fields=nope").status_code, 400)


class TestAlertTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.api = APIClient()
        self.admin = make_user(1, is_staff=True)
        self.api.force_authenticate(self.admin)

    def test_without_configuration_it_explains_what_is_missing(self):
        with patch("common.notifications.urllib.request.urlopen") as urlopen:
            response = self.api.post(TEST_ALERT)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.data["sent"])
        self.assertIn("TELEGRAM_BOT_TOKEN", response.data["detail"])
        urlopen.assert_not_called()

    @CONFIGURED
    def test_sends_now_even_when_every_event_is_switched_off(self):
        # Every event switched off (the heartbeat one too, so only the test message is sent).
        SiteConfiguration.objects.update_or_create(pk=1, defaults={name: False for name in ("alert_new_order", "alert_maintenance", "alert_cron_heartbeat")})
        with patch("common.notifications.urllib.request.urlopen", return_value=telegram_ok()) as urlopen:
            response = self.api.post(TEST_ALERT)
            again = self.api.post(TEST_ALERT)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["sent"])
        self.assertEqual(again.status_code, 429)
        (text,) = sent_texts(urlopen)
        self.assertIn(f"admin #{self.admin.pk}", text)
        self.assertNotIn(self.admin.email, text)
        self.assertNotIn("secret-token", str(response.data))

    @CONFIGURED
    def test_a_telegram_failure_is_reported(self):
        with patch("common.notifications.urllib.request.urlopen", side_effect=OSError("down")):
            response = self.api.post(TEST_ALERT)
        self.assertEqual(response.status_code, 400)
        self.assertIn("did not accept", response.data["detail"])


class WithdrawalLimitTests(TestCase):
    def setUp(self):
        self.admin = make_user(1, is_staff=True)
        self.user = make_user(2)
        Wallet.objects.create(user=self.user, balance=Decimal("1000.00000000"))
        User.objects.filter(pk=self.user.pk).update(upi_id="worker@upi")
        self.user.refresh_from_db()

    def configure(self, **values):
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"min_withdrawal_amount": Decimal("100.00"), **values})

    def withdraw(self, amount):
        return create_withdrawal_request(user=self.user, amount=Decimal(amount))

    def test_daily_maximum_counts_pending_and_approved_but_not_rejected(self):
        self.configure(withdrawal_max_per_day=Decimal("300.00"))
        first = self.withdraw("200")
        with self.assertRaisesMessage(ValidationError, "You can withdraw up to ₹300.00 per day. ₹100.00 is left for today."):
            self.withdraw("150")
        review_withdrawal_request(withdrawal_id=first.pk, reviewer=self.admin, action="reject")
        self.withdraw("300")  # the rejected request was refunded and no longer counts
        with self.assertRaises(ValidationError):
            self.withdraw("100")
        # Yesterday's requests do not count.
        WithdrawalRequest.objects.update(requested_at=timezone.now() - timedelta(days=1))
        self.withdraw("300")

    def test_no_daily_maximum_by_default(self):
        self.configure()
        for _ in range(4):
            self.withdraw("200")

    def test_first_withdrawal_is_held_before_approval(self):
        self.configure(first_withdrawal_hold_hours=24)
        first = self.withdraw("100")
        with self.assertRaisesMessage(ValidationError, "first-withdrawal hold"):
            review_withdrawal_request(withdrawal_id=first.pk, reviewer=self.admin, action="approve")
        admin_api = APIClient()
        admin_api.force_authenticate(self.admin)
        row = admin_api.get("/api/v1/admin/wallet/withdrawals/").data[0]  # an unpaginated list
        self.assertIsNotNone(row["hold_until"])
        WithdrawalRequest.objects.filter(pk=first.pk).update(requested_at=timezone.now() - timedelta(hours=25))
        review_withdrawal_request(withdrawal_id=first.pk, reviewer=self.admin, action="approve")
        # Later withdrawals are not held, and a held one can always be rejected.
        second = self.withdraw("100")
        review_withdrawal_request(withdrawal_id=second.pk, reviewer=self.admin, action="approve")

    def test_a_held_withdrawal_can_be_rejected_and_no_hold_by_default(self):
        self.configure(first_withdrawal_hold_hours=24)
        held = self.withdraw("100")
        review_withdrawal_request(withdrawal_id=held.pk, reviewer=self.admin, action="reject")
        self.configure(first_withdrawal_hold_hours=0)
        unheld = self.withdraw("100")
        review_withdrawal_request(withdrawal_id=unheld.pk, reviewer=self.admin, action="approve")
