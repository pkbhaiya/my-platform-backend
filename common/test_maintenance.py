"""Phase 10 Part C: maintenance mode (API gate, staff bypass, /api/v2, sign-up, paused claim deadlines, log)."""

from datetime import datetime, timedelta
from io import StringIO
from unittest.mock import patch

from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User
from accounts.test_registration_control import payload as signup_payload
from common.maintenance import set_maintenance
from common.models import MaintenanceEvent, SettingsChangeLog, SiteConfiguration
from common.serializers import AdminSiteConfigurationSerializer
from common.test_notifications import CONFIGURED, sent_texts, telegram_ok
from orders.testing import make_admin_wallet, unique_link
from orders.models import Order
from orders.test_provider_api import PLENTY, make_service
from tasks.models import TaskClaim
from tasks.services import claim_expires_at, claim_task, expire_stale_claims
from tasks.test_claim_lifecycle import make_order_task, make_user

MAINTENANCE = "/api/v1/admin/maintenance/"
SETTINGS = "/api/v1/admin/platform-settings/"


def bearer(user):
    return {"HTTP_AUTHORIZATION": f"Bearer {RefreshToken.for_user(user).access_token}"}


class MaintenanceGateTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.worker = make_user(1)
        self.admin = make_user(2, is_staff=True)
        SiteConfiguration.objects.update_or_create(pk=1, defaults={
            "maintenance_message": "Upgrading the servers.",
            "maintenance_expected_end": (timezone.now() + timedelta(hours=2)).replace(microsecond=0),
        })

    def test_nothing_is_blocked_while_maintenance_is_off(self):
        self.assertEqual(self.api.get("/api/v1/tasks/my/summary/", **bearer(self.worker)).status_code, 200)

    def test_user_endpoints_return_503_with_the_maintenance_notice(self):
        set_maintenance(enabled=True, by=self.admin)
        expected_end = SiteConfiguration.load().maintenance_expected_end
        for path in ("/api/v1/tasks/my/summary/", "/api/v1/tasks/available/", "/api/v1/wallet/payout-upi/",
                     "/api/v1/referrals/me/", "/api/v1/auth/me/level/"):
            response = self.api.get(path, **bearer(self.worker))
            self.assertEqual(response.status_code, 503, path)
            body = response.json()
            self.assertEqual(body["maintenance"], True)
            self.assertEqual(body["message"], "Upgrading the servers.")
            self.assertEqual(datetime.fromisoformat(body["expected_end"].replace("Z", "+00:00")), expected_end)
        # Writes are refused too, and anonymous callers get the same answer.
        self.assertEqual(self.api.post("/api/v1/tasks/1/claim/", **bearer(self.worker)).status_code, 503)
        self.assertEqual(self.api.get("/api/v1/tasks/available/").status_code, 503)
        # A malformed or invalid token is treated as a normal user, never an error.
        self.assertEqual(self.api.get("/api/v1/tasks/available/", HTTP_AUTHORIZATION="Bearer nope").status_code, 503)
        self.assertEqual(self.api.get("/api/v1/tasks/available/", HTTP_AUTHORIZATION="Bearer a b").status_code, 503)

    def test_sign_in_current_user_and_public_settings_stay_open(self):
        set_maintenance(enabled=True, by=self.admin)
        login = self.api.post("/api/v1/auth/login/", {"email": self.worker.email, "password": "TestPassword!9317"}, format="json")
        self.assertEqual(login.status_code, 200)
        refresh = self.api.post("/api/v1/auth/refresh/", {"refresh": login.data["refresh"]}, format="json")
        self.assertEqual(refresh.status_code, 200)
        me = self.api.get("/api/v1/auth/me/", HTTP_AUTHORIZATION=f"Bearer {login.data['access']}")
        self.assertEqual(me.status_code, 200)
        self.assertEqual(self.api.get("/api/v1/health/").status_code, 200)
        # Read-only means read-only: a write to an open path is still refused.
        self.assertEqual(self.api.patch("/api/v1/auth/me/", {}, format="json", HTTP_AUTHORIZATION=f"Bearer {login.data['access']}").status_code, 503)
        config = self.api.get("/api/v1/public/config/").json()
        self.assertEqual(config["maintenance"]["enabled"], True)
        self.assertEqual(config["maintenance"]["message"], "Upgrading the servers.")
        self.assertIsNotNone(config["maintenance"]["expected_end"])

    def test_staff_bypass_completely(self):
        set_maintenance(enabled=True, by=self.admin)
        self.assertEqual(self.api.get("/api/v1/tasks/my/summary/", **bearer(self.admin)).status_code, 200)
        self.assertEqual(self.api.get("/api/v1/admin/platform-settings/", **bearer(self.admin)).status_code, 200)
        self.assertEqual(self.api.get(MAINTENANCE, **bearer(self.admin)).status_code, 200)
        # A staff session (Django admin login) bypasses as well.
        session = APIClient()
        session.force_login(self.admin)
        self.assertEqual(session.get("/api/v1/admin/platform-settings/").status_code, 200)
        # A disabled staff account does not.
        User.objects.filter(pk=self.admin.pk).update(is_active=False)
        self.assertEqual(self.api.get("/api/v1/admin/platform-settings/", **bearer(self.admin)).status_code, 503)

    def test_signup_is_blocked_whatever_registration_allows(self):
        set_maintenance(enabled=True, by=self.admin)
        response = self.api.post("/api/v1/auth/signup/", signup_payload(1), format="json")
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.json()["maintenance"])
        SiteConfiguration.objects.filter(pk=1).update(registration_open=False, registration_allow_with_referral=True, referral_enabled=True)
        invited = self.api.post("/api/v1/auth/signup/", signup_payload(2, invite_code=self.worker.referral_code), format="json")
        self.assertEqual(invited.status_code, 503)
        self.assertFalse(User.objects.filter(email__in=["closed-1@example.com", "closed-2@example.com"]).exists())

    def test_signup_view_refuses_even_without_the_middleware(self):
        set_maintenance(enabled=True, by=self.admin)
        with self.modify_settings(MIDDLEWARE={"remove": "common.maintenance.MaintenanceModeMiddleware"}):
            response = self.api.post("/api/v1/auth/signup/", signup_payload(3), format="json")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(User.objects.filter(email="closed-3@example.com").exists())


@override_settings(PROVIDER_API_REQUIRE_HTTPS=False)
class ProviderAPIDuringMaintenanceTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        _client, self.key = make_admin_wallet()
        self.service = make_service()
        self.admin = make_user(2, is_staff=True)

    def call(self, **data):
        response = APIClient().post("/api/v2", {"key": self.key, **data}, format="json")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def add(self):
        with PLENTY:
            return self.call(action="add", service=self.service.pk, link=unique_link(), quantity=20)

    def test_orders_keep_arriving_when_allowed(self):
        set_maintenance(enabled=True, by=self.admin)
        result = self.add()
        self.assertIn("order", result)
        self.assertTrue(Order.objects.filter(pk=result["order"]).exists())

    def test_add_is_refused_but_other_actions_work_when_orders_are_paused(self):
        with PLENTY:
            existing = self.call(action="add", service=self.service.pk, link=unique_link(), quantity=20)["order"]
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"maintenance_accept_api_orders": False})
        self.assertIn("order", self.add())  # the switch matters only during maintenance
        set_maintenance(enabled=True, by=self.admin)
        before = Order.objects.count()
        self.assertEqual(self.add(), {"error": "Service temporarily unavailable"})
        self.assertEqual(Order.objects.count(), before)
        self.assertIsInstance(self.call(action="services"), list)
        self.assertIn("balance", self.call(action="balance"))
        self.assertEqual(self.call(action="status", order=existing)["status"], "Pending")


class PausedClaimDeadlineTests(TestCase):
    """Default claim timeout: 30 minutes."""

    def setUp(self):
        self.admin = make_user(9, is_staff=True)
        self.workers = [make_user(index) for index in (1, 2, 3)]
        _order, self.task = make_order_task(5)
        self.start = timezone.now()

    def claim(self, user, minutes_ago_of_start):
        claim_task(task_id=self.task.pk, user=user)
        claim = TaskClaim.objects.get(task=self.task, user=user)
        TaskClaim.objects.filter(pk=claim.pk).update(claimed_at=self.start - timedelta(minutes=minutes_ago_of_start))
        return claim.pk

    def status(self, claim_id):
        return TaskClaim.objects.get(pk=claim_id).status

    def test_deadlines_stand_still_during_maintenance_and_move_back_after(self):
        fresh = self.claim(self.workers[0], 10)    # 20 minutes left when maintenance starts
        overdue = self.claim(self.workers[1], 40)  # deadline passed before maintenance
        set_maintenance(enabled=True, by=self.admin, now=self.start)

        # The sweep keeps running: it expires the overdue claim, not the paused one.
        self.assertEqual(expire_stale_claims(now=self.start + timedelta(hours=3)), 1)
        self.assertEqual(self.status(overdue), TaskClaim.Status.EXPIRED)
        self.assertEqual(self.status(fresh), TaskClaim.Status.CLAIMED)

        event = set_maintenance(enabled=False, by=self.admin, now=self.start + timedelta(hours=3))
        self.assertEqual((event.duration_seconds, event.claims_extended), (3 * 3600, 1))
        claim = TaskClaim.objects.get(pk=fresh)
        self.assertEqual(claim.deadline_extension, timedelta(hours=3))
        # Still the same 20 minutes left after maintenance.
        self.assertEqual(claim_expires_at(claim), self.start + timedelta(hours=3, minutes=20))
        self.assertEqual(expire_stale_claims(now=self.start + timedelta(hours=3, minutes=19)), 0)
        self.assertEqual(expire_stale_claims(now=self.start + timedelta(hours=3, minutes=21)), 1)
        self.assertEqual(self.status(fresh), TaskClaim.Status.EXPIRED)

    def test_extensions_add_up_and_a_claim_made_during_maintenance_gets_its_paused_part(self):
        minutes = lambda value: self.start + timedelta(minutes=value)  # noqa: E731
        held = self.claim(self.workers[0], 5)  # due at +25, then +35 after the first window
        for on, off in ((0, 10), (12, 22)):  # the second window starts before the moved deadline
            set_maintenance(enabled=True, by=self.admin, now=minutes(on))
            set_maintenance(enabled=False, by=self.admin, now=minutes(off))
        self.assertEqual(TaskClaim.objects.get(pk=held).deadline_extension, timedelta(minutes=20))

        set_maintenance(enabled=True, by=self.admin, now=minutes(30))
        late = self.claim(self.workers[2], 0)
        TaskClaim.objects.filter(pk=late).update(claimed_at=minutes(40))
        set_maintenance(enabled=False, by=self.admin, now=minutes(50))
        self.assertEqual(TaskClaim.objects.get(pk=late).deadline_extension, timedelta(minutes=10))

    def test_expire_claims_command_keeps_running_during_maintenance(self):
        overdue = self.claim(self.workers[1], 40)
        set_maintenance(enabled=True, by=self.admin)
        call_command("expire_claims", stdout=StringIO())
        self.assertEqual(self.status(overdue), TaskClaim.Status.EXPIRED)
        self.assertIsNotNone(SiteConfiguration.load().expire_claims_last_run_at)


@CONFIGURED
class MaintenanceToggleTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.admin = make_user(2, is_staff=True)
        self.worker = make_user(1)

    def test_toggling_is_logged_and_alerted_without_personal_data(self):
        with patch("common.notifications.urllib.request.urlopen", return_value=telegram_ok()) as urlopen:
            with self.captureOnCommitCallbacks(execute=True):
                on = self.api.patch(SETTINGS, {"maintenance_enabled": True}, format="json", **bearer(self.admin))
            with self.captureOnCommitCallbacks(execute=True):
                again = self.api.patch(SETTINGS, {"maintenance_enabled": True}, format="json", **bearer(self.admin))
            with self.captureOnCommitCallbacks(execute=True):
                off = self.api.patch(SETTINGS, {"maintenance_enabled": False}, format="json", **bearer(self.admin))
        self.assertEqual([on.status_code, again.status_code, off.status_code], [200, 200, 200])
        self.assertTrue(on.data["maintenance_enabled"])
        self.assertIsNotNone(on.data["maintenance_started_at"])
        self.assertFalse(off.data["maintenance_enabled"])
        events = list(MaintenanceEvent.objects.order_by("id"))
        self.assertEqual([(event.enabled, event.changed_by_id) for event in events], [(True, self.admin.pk), (False, self.admin.pk)])
        self.assertEqual(self.api.get(MAINTENANCE, **bearer(self.admin)).data["events"][0]["changed_by"], self.admin.email)
        # The settings change log has the two real switches, not the repeated one.
        logged = list(SettingsChangeLog.objects.order_by("id").values_list("field", "old_value", "new_value", "changed_by"))
        self.assertEqual(logged, [("maintenance_enabled", "false", "true", self.admin.pk), ("maintenance_enabled", "true", "false", self.admin.pk)])
        texts = sent_texts(urlopen)
        self.assertEqual(len(texts), 2)
        self.assertIn(f"Maintenance mode turned ON by admin #{self.admin.pk} at ", texts[0])
        self.assertIn(f"Maintenance mode turned OFF by admin #{self.admin.pk} at ", texts[1])
        self.assertIn("0 active claim(s)", texts[1])
        for text in texts:
            self.assertNotIn(self.admin.email, text)

    def test_only_admins_can_toggle(self):
        response = self.api.patch(SETTINGS, {"maintenance_enabled": True}, format="json", **bearer(self.worker))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.api.patch(SETTINGS, {"maintenance_enabled": "maybe"}, format="json", **bearer(self.admin)).status_code, 400)
        # The status endpoint is read-only.
        self.assertEqual(self.api.post(MAINTENANCE, {"enabled": True}, format="json", **bearer(self.admin)).status_code, 405)
        self.assertFalse(SiteConfiguration.load().maintenance_enabled)
        self.assertFalse(MaintenanceEvent.objects.exists())

    def test_alert_can_be_switched_off(self):
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"alert_maintenance": False})
        with patch("common.notifications.urllib.request.urlopen", return_value=telegram_ok()) as urlopen:
            with self.captureOnCommitCallbacks(execute=True):
                set_maintenance(enabled=True, by=self.admin)
        urlopen.assert_not_called()
        self.assertEqual(MaintenanceEvent.objects.count(), 1)


class MaintenanceSettingsTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.admin = make_user(2, is_staff=True)

    def test_one_settings_save_switches_maintenance_on_with_its_message(self):
        end = (timezone.now() + timedelta(hours=1)).replace(microsecond=0)
        response = self.api.patch("/api/v1/admin/platform-settings/", {
            "maintenance_enabled": True, "maintenance_message": "  Back soon.  ",
            "maintenance_expected_end": end.isoformat(), "maintenance_accept_api_orders": False, "alert_maintenance": False,
        }, format="json", **bearer(self.admin))
        self.assertEqual(response.status_code, 200, response.data)
        config = SiteConfiguration.load()
        self.assertTrue(config.maintenance_enabled)
        self.assertEqual(MaintenanceEvent.objects.get().changed_by, self.admin)
        self.assertEqual(config.maintenance_message, "Back soon.")
        self.assertEqual(config.maintenance_expected_end, end)
        self.assertFalse(config.maintenance_accept_api_orders)
        self.assertFalse(config.alert_maintenance)

    def test_invalid_values_are_refused(self):
        response = self.api.patch("/api/v1/admin/platform-settings/", {
            "maintenance_message": " ", "maintenance_expected_end": (timezone.now() - timedelta(minutes=5)).isoformat(),
        }, format="json", **bearer(self.admin))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(set(response.data), {"maintenance_message", "maintenance_expected_end"})

    def test_saving_a_stale_settings_form_never_undoes_the_switch(self):
        stale = SiteConfiguration.load()
        set_maintenance(enabled=True, by=self.admin)
        serializer = AdminSiteConfigurationSerializer(stale, data={"company_username": "acme"}, partial=True)
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        config = SiteConfiguration.load()
        self.assertTrue(config.maintenance_enabled)
        self.assertIsNotNone(config.maintenance_started_at)
        self.assertEqual(config.company_username, "acme")

    def test_turning_off_clears_the_expected_end(self):
        SiteConfiguration.objects.update_or_create(pk=1, defaults={"maintenance_expected_end": timezone.now() + timedelta(hours=1)})
        set_maintenance(enabled=True, by=self.admin)
        set_maintenance(enabled=False, by=self.admin)
        self.assertIsNone(SiteConfiguration.load().maintenance_expected_end)
