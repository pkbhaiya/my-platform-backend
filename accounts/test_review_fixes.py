"""Regression tests for the security and correctness fixes from the October 2026 code review."""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client
from rest_framework.test import APIClient, APITestCase

User = get_user_model()
PASSWORD = "TestPassword!9317"


def make_user(email, **extra):
    fields = dict(
        email=email, password=PASSWORD, full_name="Review User",
        platform_username=email.split("@")[0], whatsapp_number="9123400000",
        state="DL", gender="female", approval_status="approved", is_active=True,
    )
    fields.update(extra)
    return User.objects.create_user(**fields)


class LoginHardeningTests(APITestCase):
    def setUp(self):
        # Throttle counters live in the default cache; isolate them per test.
        cache.clear()
        self.addCleanup(cache.clear)

    def test_login_email_is_case_insensitive(self):
        make_user("mixed-case@example.com")
        response = self.client.post(
            "/api/v1/auth/login/", {"email": "  Mixed-Case@Example.COM ", "password": PASSWORD}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["user"]["email"], "mixed-case@example.com")

    def test_repeated_failed_logins_are_throttled(self):
        payload = {"email": "nobody@example.com", "password": "wrong-password"}
        codes = [self.client.post("/api/v1/auth/login/", payload, format="json").status_code for _ in range(11)]
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10], 429)


class SignupPasswordTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_password_similar_to_the_users_own_details_is_rejected(self):
        response = self.client.post("/api/v1/auth/signup/", {
            "full_name": "Ravi Kumar", "email": "ravikumar1987@example.com",
            "platform_username": "ravikumar1987", "whatsapp_number": "9876500000",
            "state": "DL", "gender": "male",
            "password": "ravikumar1987!", "confirm_password": "ravikumar1987!",
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("password", response.data)
        self.assertIn("too similar", " ".join(response.data["password"]))
        self.assertFalse(User.objects.filter(email="ravikumar1987@example.com").exists())


class AdminUserStatusTests(APITestCase):
    def setUp(self):
        self.admin = make_user("status-admin@example.com", is_staff=True)
        self.user = make_user("status-target@example.com", approval_status="pending")
        self.client.force_authenticate(self.admin)
        self.url = f"/api/v1/admin/users/{self.user.pk}/status/"

    def test_missing_approval_status_is_a_validation_error_not_a_crash(self):
        response = self.client.patch(self.url, {"reason": "No status given"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("approval_status", response.data)

    def test_pending_is_still_not_an_allowed_target_status(self):
        response = self.client.patch(self.url, {"approval_status": "pending"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["approval_status"][0], "Status must be approved, disapproved, or disabled.",
        )

    def test_status_change_still_records_audit_fields(self):
        response = self.client.patch(self.url, {"approval_status": "approved"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.approval_status, User.ApprovalStatus.APPROVED)
        self.assertEqual(self.user.status_changed_by, self.admin)
        self.assertEqual(self.user.status_change_source, User.StatusChangeSource.ADMIN)

    def test_user_list_counts_strikes_and_approved_claims(self):
        from decimal import Decimal

        from tasks.models import Task, TaskClaim, TaskStrike

        task = Task.objects.create(target_link="https://example.com/t", reward_amount=Decimal("1"), total_slots=5, remaining_slots=2)
        other = Task.objects.create(target_link="https://example.com/u", reward_amount=Decimal("1"))
        TaskClaim.objects.create(task=task, user=self.user, status=TaskClaim.Status.APPROVED)
        TaskClaim.objects.create(task=other, user=self.user, status=TaskClaim.Status.APPROVED)
        rejected = TaskClaim.objects.create(task=task, user=self.user, status=TaskClaim.Status.REJECTED)
        TaskStrike.objects.create(task=task, claim=rejected, user=self.user, issued_by=self.admin, reason="")
        response = self.client.get("/api/v1/admin/users/", {"search": "status-target"})
        self.assertEqual(response.status_code, 200)
        strikes = response.data["results"][0]["strikes"]
        # Separate counts: the strike must not be multiplied by the two approved claims.
        self.assertEqual(strikes["count"], 1)
        detail = self.client.get(f"/api/v1/admin/users/{self.user.pk}/").data
        self.assertEqual((detail["approved_task_count"], detail["strike_count"]), (2, 1))

    def test_invalid_list_filter_message_names_every_status(self):
        response = self.client.get("/api/v1/admin/users/", {"approval_status": "bogus"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("disapproved", str(response.data["approval_status"]))


class DjangoAdminUserActionTests(APITestCase):
    def test_bulk_disable_records_who_and_why(self):
        superuser = make_user("super@example.com", is_staff=True, is_superuser=True)
        target = make_user("bulk-target@example.com")
        browser = Client()
        browser.force_login(superuser)
        response = browser.post("/admin/accounts/user/", {
            "action": "disable_selected_users", "_selected_action": [target.pk],
        })
        self.assertEqual(response.status_code, 302)
        target.refresh_from_db()
        self.assertEqual(target.approval_status, User.ApprovalStatus.DISABLED)
        self.assertFalse(target.is_active)
        self.assertEqual(target.status_changed_by, superuser)
        self.assertEqual(target.status_change_source, User.StatusChangeSource.ADMIN)
        self.assertEqual(target.status_reason, "Disabled by an administrator.")
        self.assertIsNotNone(target.status_changed_at)


class ThrottleIdentityTests(APITestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def test_a_forged_forwarded_for_header_does_not_dodge_the_login_limit(self):
        payload = {"email": "nobody@example.com", "password": "wrong-password"}
        codes = [
            self.client.post("/api/v1/auth/login/", payload, format="json", HTTP_X_FORWARDED_FOR=f"203.0.113.{index}").status_code
            for index in range(11)
        ]
        self.assertEqual(codes[-1], 429)
