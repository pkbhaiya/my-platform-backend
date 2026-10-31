from decimal import Decimal
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import is_password_usable
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import AccessToken

from orders.models import Order
from services_catalog.models import Service
from tasks.models import Task, TaskClaim
from wallet.models import Wallet, WalletTransaction

User = get_user_model()


class RegistrationAndLoginTests(APITestCase):
    def signup_payload(self, **overrides):
        payload = {
            "full_name": "New User",
            "email": "new-user@example.com",
            "platform_username": "new_user",
            "whatsapp_number": "9876543210",
            "state": "DL",
            "gender": "female",
            "password": "SafePass!928473",
            "confirm_password": "SafePass!928473",
        }
        payload.update(overrides)
        return payload

    def test_signup_creates_pending_user_with_hashed_password(self):
        response = self.client.post(
            "/api/v1/auth/signup/", self.signup_payload(), format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        user = User.objects.get(email="new-user@example.com")
        self.assertEqual(user.approval_status, User.ApprovalStatus.PENDING)
        self.assertTrue(is_password_usable(user.password))
        self.assertTrue(user.check_password("SafePass!928473"))
        self.assertNotEqual(user.password, "SafePass!928473")

    def test_signup_rejects_password_confirmation_mismatch(self):
        response = self.client.post(
            "/api/v1/auth/signup/",
            self.signup_payload(confirm_password="DifferentPass!928"),
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(email="new-user@example.com").exists())

    def test_signup_rejects_duplicate_email(self):
        User.objects.create_user(
            email="new-user@example.com",
            password="SafePass!928473",
        )

        response = self.client.post(
            "/api/v1/auth/signup/", self.signup_payload(), format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_pending_user_can_log_in_but_disabled_user_cannot(self):
        pending = User.objects.create_user(
            email="pending-login@example.com",
            password="SafePass!928473",
            full_name="Pending User",
            platform_username="pending_login",
            whatsapp_number="9876543211",
            state="DL",
            gender="female",
            approval_status=User.ApprovalStatus.PENDING,
        )

        response = self.client.post(
            "/api/v1/auth/login/",
            {"email": pending.email, "password": "SafePass!928473"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["user"]["approval_status"], "pending")

        pending.approval_status = User.ApprovalStatus.DISABLED
        pending.is_active = False
        pending.save(update_fields=["approval_status", "is_active"])
        response = self.client.post(
            "/api/v1/auth/login/",
            {"email": pending.email, "password": "SafePass!928473"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_refresh_token_returns_new_access_token_and_rejects_disabled_user(self):
        user = User.objects.create_user(
            email="refresh-user@example.com",
            password="SafePass!928473",
            full_name="Refresh User",
            platform_username="refresh_user",
            whatsapp_number="9876543212",
            state="DL",
            gender="female",
            approval_status=User.ApprovalStatus.APPROVED,
            is_active=True,
        )
        login = self.client.post(
            "/api/v1/auth/login/",
            {"email": user.email, "password": "SafePass!928473"},
            format="json",
        )

        refreshed = self.client.post(
            "/api/v1/auth/refresh/",
            {"refresh": login.data["refresh"]},
            format="json",
        )
        self.assertEqual(refreshed.status_code, status.HTTP_200_OK)
        self.assertIn("access", refreshed.data)

        user.approval_status = User.ApprovalStatus.DISABLED
        user.is_active = False
        user.save(update_fields=["approval_status", "is_active"])
        refreshed = self.client.post(
            "/api/v1/auth/refresh/",
            {"refresh": login.data["refresh"]},
            format="json",
        )
        self.assertEqual(refreshed.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_expired_access_token_is_rejected(self):
        user = User.objects.create_user(
            email="expired-token@example.com",
            password="SafePass!928473",
            full_name="Expired Token User",
            platform_username="expired_token",
            whatsapp_number="9876543213",
            state="DL",
            gender="female",
            approval_status=User.ApprovalStatus.APPROVED,
            is_active=True,
        )
        token = AccessToken.for_user(user)
        token.set_exp(lifetime=timedelta(seconds=-1))
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        response = self.client.get("/api/v1/auth/me/")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class AdminUserListAPITests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin-list-test@example.com",
            password="TestAdminPassword123!",
            full_name="Test Admin",
            platform_username="test_admin",
            whatsapp_number="9000000001",
            state="DL",
            gender="female",
            approval_status="approved",
            is_staff=True,
        )

        self.pending_user = User.objects.create_user(
            email="pending-list-test@example.com",
            password="TestUserPassword123!",
            full_name="Pending User",
            platform_username="pending_user",
            whatsapp_number="9000000002",
            state="DL",
            gender="female",
            approval_status="pending",
        )

        self.approved_user = User.objects.create_user(
            email="approved-list-test@example.com",
            password="TestUserPassword123!",
            full_name="Approved User",
            platform_username="approved_user",
            whatsapp_number="9000000003",
            state="DL",
            gender="female",
            approval_status="approved",
        )

        self.url = "/api/v1/admin/users/"

    def test_admin_can_list_users(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 3)

    def test_admin_can_filter_pending_users(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(
            self.url,
            {"approval_status": "pending"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["results"][0]["approval_status"],
            "pending",
        )

    def test_admin_can_filter_approved_users(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(
            self.url,
            {"approval_status": "approved"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)

    def test_admin_filter_rejects_invalid_status(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(
            self.url,
            {"approval_status": "invalid"},
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_regular_user_cannot_list_users(self):
        self.client.force_authenticate(user=self.pending_user)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_combine_status_filter_and_pagination(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(
            "/api/v1/admin/users/?approval_status=approved&page=1&page_size=1"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 2)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertIn("next", response.data)
        self.assertIn("previous", response.data)

    def test_admin_can_search_users_by_email_username_or_id_with_status_filter(self):
        self.client.force_authenticate(user=self.admin)
        for term, expected_id in (("PENDING-LIST-TEST", self.pending_user.id), ("pending_user", self.pending_user.id), (str(self.approved_user.id), self.approved_user.id)):
            response = self.client.get(self.url, {"search": term, "approval_status": "pending" if expected_id == self.pending_user.id else "approved", "page": 1, "page_size": 1})
            self.assertEqual(response.status_code, status.HTTP_200_OK)
            self.assertEqual(response.data["count"], 1)
            self.assertEqual(response.data["results"][0]["id"], expected_id)

    def test_admin_can_view_user_details(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(f"/api/v1/admin/users/{self.pending_user.id}/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data["email"],
            self.pending_user.email,
        )
        self.assertEqual(
            response.data["approval_status"],
            "pending",
        )
        self.assertNotIn("password", response.data)

    def test_regular_user_cannot_view_user_details(self):
        self.client.force_authenticate(user=self.pending_user)

        response = self.client.get(f"/api/v1/admin/users/{self.approved_user.id}/")

        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_admin_can_view_user_activity_and_credited_rewards(self):
        self.client.force_authenticate(user=self.admin)

        service = Service.objects.create(
            name="Activity Test Service",
            description="Test service",
            price_per_1000=Decimal("100.0000"),
            minimum_order_quantity=1,
            capacity_percentage=Decimal("100.00"),
            eligible_level="all",
            business_share_percentage=Decimal("75.00"),
            user_reward_percentage=Decimal("25.00"),
            order_source="both",
            target_link_required=True,
        )

        order = Order.objects.create(
            service=service,
            source="api",
            target_link="https://example.com/activity-test",
            quantity=1,
            price_per_1000_snapshot=Decimal("100.0000"),
            business_share_percentage_snapshot=Decimal("75.00"),
            user_reward_percentage_snapshot=Decimal("25.00"),
            capacity_percentage_snapshot=Decimal("100.00"),
            eligible_user_count_at_acceptance=1,
            capacity_limit_at_acceptance=1,
            total_order_value=Decimal("0.10000000"),
            fulfillment_status=Order.FulfillmentStatus.ACCEPTED,
        )

        task = Task.objects.create(
            order=order,
            unit_number=1,
            target_link=order.target_link,
            price_per_1000_snapshot=Decimal("100.0000"),
            user_reward_percentage_snapshot=Decimal("25.00"),
            reward_amount=Decimal("0.02500000"),
            status=Task.Status.APPROVED,
            assigned_user=self.pending_user,
        )
        claim = TaskClaim.objects.create(task=task, user=self.pending_user, status=TaskClaim.Status.APPROVED, submitted_result="work_done")

        wallet = Wallet.objects.create(
            user=self.pending_user,
            balance=Decimal("0.02500000"),
        )

        WalletTransaction.objects.create(
            wallet=wallet,
            transaction_type=WalletTransaction.TransactionType.TASK_REWARD,
            direction=WalletTransaction.Direction.CREDIT,
            amount=Decimal("0.02500000"),
            balance_after=Decimal("0.02500000"),
            task=task,
            claim=claim,
            description="Approved task reward",
        )

        response = self.client.get(
            f"/api/v1/admin/users/{self.pending_user.id}/activity/"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["tasks"]["total_assigned"], 1)
        self.assertEqual(response.data["tasks"]["approved"], 1)
        self.assertEqual(response.data["tasks"]["pending_review"], 0)
        self.assertEqual(response.data["wallet"]["balance"], "0.02500000")
        self.assertEqual(
            response.data["wallet"]["total_task_rewards_credited"],
            "0.02500000",
        )

    def test_regular_user_cannot_view_user_activity(self):
        self.client.force_authenticate(user=self.pending_user)

        response = self.client.get(
            f"/api/v1/admin/users/{self.approved_user.id}/activity/"
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_admin_can_view_user_task_history(self):
        self.client.force_authenticate(user=self.admin)

        service = Service.objects.create(
            name="User Task History Test",
            description="Test service",
            price_per_1000=Decimal("100.0000"),
            minimum_order_quantity=1,
            capacity_percentage=Decimal("100.00"),
            eligible_level="all",
            business_share_percentage=Decimal("75.00"),
            user_reward_percentage=Decimal("25.00"),
            order_source="both",
            target_link_required=True,
        )

        order = Order.objects.create(
            service=service,
            source="api",
            target_link="https://example.com/history-test",
            quantity=1,
            price_per_1000_snapshot=Decimal("100.0000"),
            business_share_percentage_snapshot=Decimal("75.00"),
            user_reward_percentage_snapshot=Decimal("25.00"),
            capacity_percentage_snapshot=Decimal("100.00"),
            eligible_user_count_at_acceptance=1,
            capacity_limit_at_acceptance=1,
            total_order_value=Decimal("0.10000000"),
            fulfillment_status=Order.FulfillmentStatus.ACCEPTED,
        )

        task = Task.objects.create(
            order=order,
            unit_number=1,
            target_link=order.target_link,
            price_per_1000_snapshot=Decimal("100.0000"),
            user_reward_percentage_snapshot=Decimal("25.00"),
            reward_amount=Decimal("0.02500000"),
            status=Task.Status.APPROVED,
            assigned_user=self.pending_user,
        )
        claim = TaskClaim.objects.create(task=task, user=self.pending_user, status=TaskClaim.Status.APPROVED, submitted_result="work_done")

        response = self.client.get(
            f"/api/v1/admin/users/{self.pending_user.id}/tasks/"
            "?status=approved&page=1&page_size=1"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertEqual(response.data["results"][0]["task_id"], task.id)
        self.assertEqual(
            response.data["results"][0]["status"],
            TaskClaim.Status.APPROVED,
        )
        self.assertEqual(
            response.data["results"][0]["reward_amount"],
            "0.02500000",
        )

    def test_regular_user_cannot_view_user_task_history(self):
        self.client.force_authenticate(user=self.pending_user)

        response = self.client.get(
            f"/api/v1/admin/users/{self.approved_user.id}/tasks/"
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
        )


class AdminOperationsReadAndLevelTests(APITestCase):
    def setUp(self):
        from .models import DuplicateRegistrationFlag, UserLevel

        self.UserLevel = UserLevel
        self.DuplicateRegistrationFlag = DuplicateRegistrationFlag
        self.admin = User.objects.create_user(
            email="ops-admin@example.com", password="TestPassword!9317",
            full_name="Operations Admin", platform_username="ops_admin",
            whatsapp_number="9876500001", state="DL", gender="female",
            is_staff=True,
        )
        self.user = User.objects.create_user(
            email="ops-user@example.com", password="TestPassword!9317",
            full_name="Operations User", platform_username="ops_user",
            whatsapp_number="9876500002", state="DL", gender="female",
        )
        self.matched = User.objects.create_user(
            email="matched-user@example.com", password="TestPassword!9317",
            full_name="Matched User", platform_username="matched_user",
            whatsapp_number="9876500003", state="DL", gender="female",
        )
        self.level = UserLevel.objects.create(
            code="starter", name="Starter", minimum_approved_tasks=0,
            display_order=1, badge_color="#64748B", is_active=True,
        )

    def test_admin_can_list_create_and_update_levels(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/v1/admin/user-levels/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data[0]["code"], "starter")
        response = self.client.post("/api/v1/admin/user-levels/", {
            "code": "gold", "name": "Gold", "minimum_approved_tasks": 10,
            "display_order": 2, "badge_color": "#D4AF37", "is_active": True,
        }, format="json")
        self.assertEqual(response.status_code, 201)
        level_id = response.data["id"]
        response = self.client.patch(f"/api/v1/admin/user-levels/{level_id}/", {"name": "Gold Plus"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["name"], "Gold Plus")

    def test_normal_user_cannot_manage_levels_or_view_duplicate_flags(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.get("/api/v1/admin/user-levels/").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/admin/duplicate-registrations/").status_code, 403)

    def test_admin_sees_derived_user_level(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/v1/admin/users/{self.user.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["approved_task_count"], 0)
        self.assertEqual(response.data["current_level"]["code"], "starter")

    def test_admin_can_filter_duplicate_flags_without_sensitive_phone_fields(self):
        flag = self.DuplicateRegistrationFlag.objects.create(
            new_user=self.user, matched_user=self.matched,
            match_type="platform_username", status="pending",
        )
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/v1/admin/duplicate-registrations/?match_type=platform_username&status=pending")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], flag.id)
        self.assertNotIn("whatsapp_number", response.data["results"][0]["new_user"])
