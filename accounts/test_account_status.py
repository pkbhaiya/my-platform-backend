from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient, APITestCase

from accounts.models import UserLevel
from common.models import SiteConfiguration
from common.serializers import whatsapp_url
from tasks.models import Task, TaskClaim

User = get_user_model()
PASSWORD = "TestPassword!9317"
STATUS_URL = "/api/v1/auth/me/account-status/"


def make_user(index, **extra):
    fields = dict(
        email=f"status-user-{index}@example.com", password=PASSWORD,
        full_name=f"Status User {index}", platform_username=f"status_user_{index}",
        whatsapp_number=f"92345{index:05d}", state="DL", gender="female",
        approval_status="approved", is_active=True,
    )
    fields.update(extra)
    return User.objects.create_user(**fields)


class AccountStatusTestCase(APITestCase):
    def setUp(self):
        UserLevel.objects.create(code="bronze", name="Bronze", minimum_approved_tasks=0, display_order=1, strike_limit=3)
        self.config, _ = SiteConfiguration.objects.get_or_create(pk=1)
        self.config.approval_title = "Your Account Is Awaiting Approval"
        self.config.approval_instructions = "Follow our official profile, then message us with your registered email."
        self.config.disabled_account_message = "This account was disabled for breaking the community rules."
        self.config.save()
        self.admin = make_user(0, is_staff=True)
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)

    def status_for(self, user):
        client = APIClient()
        client.force_authenticate(User.objects.get(pk=user.pk))
        response = client.get(STATUS_URL)
        self.assertEqual(response.status_code, 200)
        return response.data

    def reject_new_submission(self, user):
        task = Task.objects.create(target_link="https://example.com/t", reward_amount=Decimal("1"), remaining_slots=0, status=Task.Status.COMPLETED)
        claim = TaskClaim.objects.create(task=task, user=user, status=TaskClaim.Status.WORK_SUBMITTED, submitted_result="work_done")
        response = self.admin_client.post(f"/api/v1/admin/task-submissions/{claim.pk}/review/", {"action": "reject"}, format="json")
        self.assertEqual(response.status_code, 200)


class AccountStatusEndpointTests(AccountStatusTestCase):
    def test_new_pending_user_gets_approval_instructions_and_no_strike_reason(self):
        pending = make_user(1, approval_status="pending")
        data = self.status_for(pending)
        self.assertEqual(data["approval_status"], "pending")
        self.assertFalse(data["task_access"])
        self.assertEqual((data["status_reason"], data["status_change_source"]), ("", ""))
        self.assertEqual(data["strikes"]["strike_count"], 0)
        self.assertFalse(data["strikes"]["strike_limit_reached"])
        self.assertEqual(data["support"]["approval_title"], "Your Account Is Awaiting Approval")
        self.assertEqual(data["support"]["approval_instructions"], self.config.approval_instructions)

    def test_strike_limit_disapproval_reports_real_level_count_limit_and_reason(self):
        user = make_user(1)
        for _ in range(3):
            self.reject_new_submission(user)
        data = self.status_for(user)
        self.assertEqual(data["approval_status"], "disapproved")
        self.assertEqual(data["status_change_source"], "strike_limit")
        self.assertEqual(data["status_reason"], "Strike limit reached: 3 strike(s) at Bronze (limit 3).")
        self.assertEqual(data["strikes"]["current_level"]["name"], "Bronze")
        self.assertEqual((data["strikes"]["strike_count"], data["strikes"]["strike_limit"]), (3, 3))
        UserLevel.objects.filter(code="bronze").update(strike_limit=8)
        self.assertEqual(self.status_for(user)["strikes"]["strike_limit"], 8)

    def test_manual_disapproval_keeps_the_admins_reason_and_is_not_attributed_to_strikes(self):
        user = make_user(1)
        response = self.admin_client.patch(
            f"/api/v1/admin/users/{user.pk}/status/",
            {"approval_status": "disapproved", "reason": "Duplicate account detected during review."},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        data = self.status_for(user)
        self.assertEqual(data["approval_status"], "disapproved")
        self.assertEqual(data["status_change_source"], "admin")
        self.assertEqual(data["status_reason"], "Duplicate account detected during review.")
        self.assertEqual(data["strikes"]["strike_count"], 0)
        self.assertFalse(data["strikes"]["strike_limit_reached"])
        detail = self.admin_client.get(f"/api/v1/admin/users/{user.pk}/").data
        self.assertEqual((detail["status_change_source"], detail["status_changed_by"]), ("admin", self.admin.email))

    def test_manual_disapproval_without_reason_uses_a_neutral_reason(self):
        user = make_user(1)
        self.admin_client.patch(f"/api/v1/admin/users/{user.pk}/status/", {"approval_status": "disapproved"}, format="json")
        data = self.status_for(user)
        self.assertEqual(data["status_reason"], "Disapproved by an administrator.")
        self.assertNotIn("strike", data["status_reason"].lower())

    def test_approved_user_has_task_access(self):
        user = make_user(1)
        data = self.status_for(user)
        self.assertEqual((data["approval_status"], data["task_access"]), ("approved", True))
        client = APIClient()
        client.force_authenticate(user)
        self.assertEqual(client.get("/api/v1/tasks/available/").status_code, 200)

    def test_status_endpoint_requires_authentication(self):
        self.assertEqual(APIClient().get(STATUS_URL).status_code, 401)


class RestrictedAccessTests(AccountStatusTestCase):
    def test_pending_and_disapproved_users_cannot_reach_task_apis_directly(self):
        task = Task.objects.create(target_link="https://example.com/open", total_slots=3, remaining_slots=3, reward_amount=Decimal("1"))
        pending = make_user(1, approval_status="pending")
        disapproved = make_user(2, approval_status="disapproved", status_reason="Duplicate account.", status_change_source="admin")
        for user in (pending, disapproved):
            client = APIClient()
            client.force_authenticate(user)
            self.assertEqual(client.get("/api/v1/tasks/available/").status_code, 403)
            self.assertEqual(client.get("/api/v1/tasks/my/").status_code, 403)
            self.assertEqual(client.post(f"/api/v1/tasks/{task.pk}/claim/", {}, format="json").status_code, 403)
            self.assertEqual(client.post(f"/api/v1/tasks/{task.pk}/open-target/", {}, format="json").status_code, 403)
            self.assertEqual(client.post(f"/api/v1/tasks/{task.pk}/submit/", {"result": "work_done"}, format="json").status_code, 403)
        task.refresh_from_db()
        self.assertEqual(task.remaining_slots, 3)
        self.assertFalse(TaskClaim.objects.exists())

    def test_wallet_visibility_follows_existing_permissions(self):
        pending = make_user(1, approval_status="pending")
        disapproved = make_user(2, approval_status="disapproved")
        for user, expected in ((pending, 403), (disapproved, 200)):
            client = APIClient()
            client.force_authenticate(user)
            self.assertEqual(client.get("/api/v1/wallet/").status_code, expected)
            self.assertEqual(client.get("/api/v1/wallet/transactions/").status_code, expected)
            self.assertEqual(client.get("/api/v1/wallet/withdrawals/").status_code, expected)


class DisabledAccountMessageTests(AccountStatusTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user(1)

    def login(self, password=PASSWORD, email=None):
        return APIClient().post("/api/v1/auth/login/", {"email": email or self.user.email, "password": password}, format="json")

    def disable(self):
        response = self.admin_client.patch(f"/api/v1/admin/users/{self.user.pk}/status/", {"approval_status": "disabled"}, format="json")
        self.assertEqual(response.status_code, 200)

    def test_disabled_login_with_correct_password_shows_configured_message(self):
        self.disable()
        response = self.login()
        self.assertEqual(response.status_code, 401)
        self.assertEqual(str(response.data["detail"]), self.config.disabled_account_message)
        self.assertEqual(response.data["code"], "account_disabled")
        self.assertNotIn("access", response.data)

    def test_wrong_password_never_reveals_disabled_status(self):
        self.disable()
        for response in (self.login(password="Wrong-Password-1"), self.login(email="nobody@example.com")):
            self.assertEqual(response.status_code, 401)
            self.assertNotEqual(str(response.data["detail"]), self.config.disabled_account_message)

    def test_refresh_after_disabling_returns_configured_message(self):
        refresh = self.login().data["refresh"]
        self.disable()
        response = APIClient().post("/api/v1/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(str(response.data["detail"]), self.config.disabled_account_message)

    def test_disapproved_and_pending_users_can_still_sign_in(self):
        for status in ("pending", "disapproved"):
            User.objects.filter(pk=self.user.pk).update(approval_status=status)
            response = self.login()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data["user"]["approval_status"], status)


class SiteConfigurationInPanelTests(AccountStatusTestCase):
    def test_admin_updates_are_reflected_in_account_status_and_public_endpoint(self):
        payload = {
            "company_profile_image_url": "https://cdn.example.com/logo.png",
            "company_username": "official_company",
            "whatsapp_contact": "+91 98765 43210",
            "approval_title": "Almost there",
            "approval_instructions": "Send your registered email to our WhatsApp support.",
            "disabled_account_message": "Disabled after repeated policy violations.",
        }
        response = self.admin_client.patch("/api/v1/admin/platform-settings/", payload, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["whatsapp_url"], "https://wa.me/919876543210")
        support = self.status_for(make_user(1, approval_status="pending"))["support"]
        for key, value in payload.items():
            self.assertEqual(support[key], value)
        self.assertEqual(support["whatsapp_url"], "https://wa.me/919876543210")
        public = APIClient().get("/api/v1/approval-instructions/").data
        self.assertEqual(public["company_username"], "official_company")
        self.assertEqual(public["whatsapp_url"], "https://wa.me/919876543210")

    def test_missing_contact_settings_produce_no_link_and_do_not_break_the_endpoint(self):
        SiteConfiguration.objects.filter(pk=1).update(company_profile_image_url="", company_username="", whatsapp_contact="")
        support = self.status_for(make_user(1, approval_status="pending"))["support"]
        self.assertEqual((support["company_username"], support["whatsapp_contact"], support["company_profile_image_url"]), ("", "", ""))
        self.assertIsNone(support["whatsapp_url"])

    def test_ambiguous_whatsapp_numbers_are_rejected_and_never_linked(self):
        for value in ("9876543210", "+0 12345678", "call +91 98765", "+91-98765", "++919876543210", "+1234567890123456"):
            self.assertIsNone(whatsapp_url(value), value)
            response = self.admin_client.patch("/api/v1/admin/platform-settings/", {"whatsapp_contact": value}, format="json")
            self.assertEqual(response.status_code, 400, value)
        self.assertEqual(whatsapp_url("+1 (415) 555-0100"), "https://wa.me/14155550100")
        self.assertEqual(self.admin_client.patch("/api/v1/admin/platform-settings/", {"whatsapp_contact": ""}, format="json").status_code, 200)

    def test_disabled_message_is_editable_but_cannot_be_blank(self):
        url = "/api/v1/admin/platform-settings/"
        self.assertEqual(self.admin_client.patch(url, {"disabled_account_message": "   "}, format="json").status_code, 400)
        self.assertEqual(self.admin_client.patch(url, {"disabled_account_message": "New disabled text."}, format="json").status_code, 200)
        self.assertEqual(SiteConfiguration.objects.get(pk=1).disabled_account_message, "New disabled text.")
