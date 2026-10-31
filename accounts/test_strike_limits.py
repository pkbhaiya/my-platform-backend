from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TransactionTestCase
from rest_framework.test import APIClient, APITestCase

from accounts.models import UserLevel
from tasks.models import Task, TaskClaim, TaskStrike
from tasks.services import claim_task, reject_submitted_task
from wallet.models import Wallet, WalletTransaction

User = get_user_model()
PASSWORD = "TestPassword!9317"


def make_user(index, **extra):
    fields = dict(
        email=f"strike-user-{index}@example.com", password=PASSWORD,
        full_name=f"Strike User {index}", platform_username=f"strike_user_{index}",
        whatsapp_number=f"91234{index:05d}", state="DL", gender="female",
        approval_status="approved", is_active=True,
    )
    fields.update(extra)
    return User.objects.create_user(**fields)


def submitted_claim(user, result="work_done"):
    status = TaskClaim.Status.WORK_SUBMITTED if result == "work_done" else TaskClaim.Status.POST_NOT_AVAILABLE
    task = Task.objects.create(target_link="https://example.com/strike", reward_amount=Decimal("1"), remaining_slots=0, status=Task.Status.COMPLETED)
    return TaskClaim.objects.create(task=task, user=user, status=status, submitted_result=result)


def give_approved_claims(user, count):
    for _ in range(count):
        task = Task.objects.create(target_link="https://example.com/approved", remaining_slots=0, status=Task.Status.COMPLETED)
        TaskClaim.objects.create(task=task, user=user, status=TaskClaim.Status.APPROVED, submitted_result="work_done")


def create_ladder():
    return {
        code: UserLevel.objects.create(code=code, name=code.title(), minimum_approved_tasks=threshold, display_order=order, strike_limit=limit)
        for order, (code, threshold, limit) in enumerate(
            [("bronze", 0, 3), ("silver", 5, 5), ("gold", 10, 7), ("diamond", 20, 10)], start=1)
    }


class StrikeLimitTestCase(APITestCase):
    def setUp(self):
        self.levels = create_ladder()
        self.admin = make_user(0, is_staff=True)
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)

    def reject(self, claim):
        return self.admin_client.post(f"/api/v1/admin/task-submissions/{claim.pk}/review/", {"action": "reject"}, format="json")

    def status_of(self, user):
        user.refresh_from_db()
        return user.approval_status


class StrikeSettingsAPITests(StrikeLimitTestCase):
    def test_each_level_keeps_its_own_limit_and_admin_edits_persist(self):
        listed = {row["code"]: row["strike_limit"] for row in self.admin_client.get("/api/v1/admin/user-levels/").data}
        self.assertEqual(listed, {"bronze": 3, "silver": 5, "gold": 7, "diamond": 10})
        response = self.admin_client.patch(f"/api/v1/admin/user-levels/{self.levels['silver'].pk}/", {"strike_limit": 6}, format="json")
        self.assertEqual(response.status_code, 200)
        refreshed = {row["code"]: row["strike_limit"] for row in self.admin_client.get("/api/v1/admin/user-levels/").data}
        self.assertEqual(refreshed["silver"], 6)
        self.assertEqual(refreshed["bronze"], 3)

    def test_invalid_limits_are_rejected_and_not_saved(self):
        url = f"/api/v1/admin/user-levels/{self.levels['bronze'].pk}/"
        for value in (0, -1, 1001, "abc", None, 2.5):
            response = self.admin_client.patch(url, {"strike_limit": value}, format="json")
            self.assertEqual(response.status_code, 400, value)
            self.assertIn("strike_limit", response.data)
        self.levels["bronze"].refresh_from_db()
        self.assertEqual(self.levels["bronze"].strike_limit, 3)

    def test_non_admin_cannot_change_limits(self):
        client = APIClient()
        client.force_authenticate(make_user(1))
        response = client.patch(f"/api/v1/admin/user-levels/{self.levels['bronze'].pk}/", {"strike_limit": 99}, format="json")
        self.assertEqual(response.status_code, 403)


class AutomaticDisapprovalTests(StrikeLimitTestCase):
    def test_bronze_user_is_disapproved_on_the_third_eligible_rejection(self):
        user = make_user(1)
        for expected in ("approved", "approved"):
            self.assertEqual(self.reject(submitted_claim(user)).status_code, 200)
            self.assertEqual(self.status_of(user), expected)
        response = self.reject(submitted_claim(user))
        self.assertEqual(response.data["account_status"], "disapproved")
        self.assertEqual(self.status_of(user), "disapproved")
        self.assertTrue(user.is_active)
        self.assertIn("Strike limit reached: 3 strike(s) at Bronze (limit 3)", user.status_reason)
        self.assertIsNotNone(user.status_changed_at)
        self.assertEqual(TaskStrike.objects.filter(user=user).count(), 3)

    def test_other_levels_use_their_own_limits(self):
        silver = make_user(1)
        give_approved_claims(silver, 5)
        for _ in range(4):
            self.reject(submitted_claim(silver))
        self.assertEqual(self.status_of(silver), "approved")
        self.reject(submitted_claim(silver))
        self.assertEqual(self.status_of(silver), "disapproved")
        self.assertIn("at Silver (limit 5)", silver.status_reason)

    def test_repeated_rejection_creates_one_strike(self):
        user = make_user(1)
        claim = submitted_claim(user)
        self.assertEqual(self.reject(claim).status_code, 200)
        self.assertEqual(self.reject(claim).status_code, 400)
        self.assertEqual(self.reject(claim).status_code, 400)
        self.assertEqual(TaskStrike.objects.filter(claim=claim).count(), 1)
        self.assertEqual(self.status_of(user), "approved")

    def test_post_not_available_creates_no_strike(self):
        user = make_user(1)
        for _ in range(3):
            claim = submitted_claim(user, result="post_not_available")
            self.assertEqual(self.reject(claim).status_code, 400)
        self.assertFalse(TaskStrike.objects.filter(user=user).exists())
        self.assertEqual(self.status_of(user), "approved")

    def test_upgrading_before_the_limit_applies_the_new_levels_limit_and_keeps_strikes(self):
        user = make_user(1)
        self.reject(submitted_claim(user))
        self.reject(submitted_claim(user))
        give_approved_claims(user, 5)  # Bronze -> Silver (limit 5)
        self.assertEqual(TaskStrike.objects.filter(user=user).count(), 2)
        self.reject(submitted_claim(user))
        self.reject(submitted_claim(user))
        self.assertEqual(self.status_of(user), "approved")
        self.reject(submitted_claim(user))
        self.assertEqual(self.status_of(user), "disapproved")
        self.assertEqual(TaskStrike.objects.filter(user=user).count(), 5)

    def test_disapproved_account_is_not_restored_by_level_or_limit_changes(self):
        user = make_user(1)
        for _ in range(3):
            self.reject(submitted_claim(user))
        self.assertEqual(self.status_of(user), "disapproved")
        give_approved_claims(user, 25)  # now Diamond, limit 10
        self.admin_client.patch(f"/api/v1/admin/user-levels/{self.levels['bronze'].pk}/", {"strike_limit": 50}, format="json")
        pending = submitted_claim(user)
        self.admin_client.post(f"/api/v1/admin/task-submissions/{pending.pk}/review/", {"action": "approve"}, format="json")
        self.assertEqual(self.status_of(user), "disapproved")
        self.assertEqual(TaskStrike.objects.filter(user=user).count(), 3)

    def test_missing_level_configuration_never_means_unlimited_strikes(self):
        UserLevel.objects.all().delete()
        user = make_user(1)
        for _ in range(3):
            self.reject(submitted_claim(user))
        self.assertEqual(self.status_of(user), "disapproved")

    def test_user_below_every_active_level_gets_the_most_restrictive_limit(self):
        UserLevel.objects.filter(code="bronze").update(is_active=False, strike_limit=2)
        user = make_user(1)  # no active level matches 0 approved claims
        self.reject(submitted_claim(user))
        self.reject(submitted_claim(user))
        self.assertEqual(self.status_of(user), "disapproved")

    def test_administrator_accounts_are_never_auto_disapproved(self):
        reviewer = make_user(2, is_staff=True)
        for _ in range(4):
            reject_submitted_task(task_id=submitted_claim(reviewer).pk, reviewer=self.admin)
        self.assertEqual(self.status_of(reviewer), "approved")


class DisapprovedUserAccessTests(StrikeLimitTestCase):
    def setUp(self):
        super().setUp()
        self.user = make_user(1)
        self.wallet = Wallet.objects.create(user=self.user, balance=Decimal("12.50000000"))
        give_approved_claims(self.user, 1)
        earned = TaskClaim.objects.get(user=self.user, status=TaskClaim.Status.APPROVED)
        self.ledger = WalletTransaction.objects.create(
            wallet=self.wallet, transaction_type=WalletTransaction.TransactionType.TASK_REWARD,
            direction=WalletTransaction.Direction.CREDIT, amount=Decimal("12.50000000"),
            balance_after=Decimal("12.50000000"), task=earned.task, claim=earned, description="Earlier reward",
        )
        self.open_task = Task.objects.create(target_link="https://example.com/open", total_slots=5, remaining_slots=5, reward_amount=Decimal("1"))
        for _ in range(3):
            self.reject(submitted_claim(self.user))
        self.user.refresh_from_db()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_disapproved_user_cannot_claim_or_use_task_endpoints(self):
        self.assertEqual(self.status_of(self.user), "disapproved")
        self.assertEqual(self.client.post(f"/api/v1/tasks/{self.open_task.pk}/claim/", {}, format="json").status_code, 403)
        for path in ("/api/v1/tasks/available/", "/api/v1/tasks/my/"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 403)
            self.assertIn("strike limit", str(response.data["detail"]))
        self.assertEqual(self.client.post(f"/api/v1/tasks/{self.open_task.pk}/open-target/", {}, format="json").status_code, 403)
        with self.assertRaisesMessage(ValidationError, "strike limit"):
            claim_task(task_id=self.open_task.pk, user=self.user)
        self.open_task.refresh_from_db()
        self.assertEqual(self.open_task.remaining_slots, 5)
        self.assertFalse(TaskClaim.objects.filter(task=self.open_task).exists())

    def test_disapproved_user_can_sign_in_and_read_but_not_write_account_records(self):
        login = APIClient().post("/api/v1/auth/login/", {"email": self.user.email, "password": PASSWORD}, format="json")
        self.assertEqual(login.status_code, 200)
        refreshed = APIClient().post("/api/v1/auth/refresh/", {"refresh": login.data["refresh"]}, format="json")
        self.assertEqual(refreshed.status_code, 200)
        me = self.client.get("/api/v1/auth/me/").data
        self.assertEqual(me["approval_status"], "disapproved")
        self.assertIn("Strike limit reached", me["status_reason"])
        self.assertEqual((me["strikes"]["strike_count"], me["strikes"]["strike_limit"]), (3, 3))
        self.assertEqual(self.client.get("/api/v1/wallet/").data["balance"], "12.50000000")
        self.assertEqual(self.client.get("/api/v1/wallet/transactions/").data["count"], 1)
        self.assertEqual(self.client.get("/api/v1/wallet/withdrawals/").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/auth/me/platform-usernames/").status_code, 200)
        self.assertEqual(self.client.post("/api/v1/wallet/withdrawals/", {"amount": "1", "payout_method": "upi", "payout_details": "name@bank"}, format="json").status_code, 403)
        self.assertEqual(self.client.post("/api/v1/auth/me/platform-usernames/", {"platform_name": "x", "username": "y"}, format="json").status_code, 403)
        review = self.client.get("/api/v1/tasks/my/review-status/").data
        self.assertEqual((review["approval_status"], review["strike_count"], review["strike_limit"]), ("disapproved", 3, 3))

    def test_account_wallet_ledger_and_strikes_are_preserved(self):
        self.user.refresh_from_db()
        self.assertTrue(User.objects.filter(pk=self.user.pk, is_active=True).exists())
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, Decimal("12.50000000"))
        self.assertTrue(WalletTransaction.objects.filter(pk=self.ledger.pk).exists())
        self.assertEqual(TaskStrike.objects.filter(user=self.user).count(), 3)
        self.assertEqual(TaskClaim.objects.filter(user=self.user, status=TaskClaim.Status.REJECTED).count(), 3)

    def test_admin_sees_strike_data_and_can_manually_reapprove_without_losing_history(self):
        listed = self.admin_client.get(f"/api/v1/admin/users/?search={self.user.email}").data["results"]
        self.assertEqual(len(listed), 1)
        row = listed[0]
        self.assertEqual(row["approval_status"], "disapproved")
        self.assertEqual(row["level"]["code"], "bronze")
        self.assertEqual(row["strikes"], {"count": 3, "limit": 3, "reached": True})
        self.assertIn("Strike limit reached", self.admin_client.get(f"/api/v1/admin/users/{self.user.pk}/").data["status_reason"])
        strikes = self.admin_client.get(f"/api/v1/admin/task-strikes/?user_id={self.user.pk}").data
        self.assertEqual(strikes["count"], 3)

        normal = APIClient()
        normal.force_authenticate(make_user(5))
        self.assertEqual(normal.patch(f"/api/v1/admin/users/{self.user.pk}/status/", {"approval_status": "approved"}, format="json").status_code, 403)

        response = self.admin_client.patch(f"/api/v1/admin/users/{self.user.pk}/status/", {"approval_status": "approved"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.status_of(self.user), "approved")
        self.assertTrue(self.user.is_active)
        self.assertEqual((self.user.status_reason, self.user.status_change_source), ("Approved by an administrator.", "admin"))
        self.assertEqual(TaskStrike.objects.filter(user=self.user).count(), 3)
        self.assertEqual(self.client.post(f"/api/v1/tasks/{self.open_task.pk}/claim/", {}, format="json").status_code, 200)
        # Enforcement still applies after reapproval: the next rejection counts.
        self.reject(submitted_claim(self.user))
        self.assertEqual(self.status_of(self.user), "disapproved")

    def test_admin_can_manually_disapprove_without_disabling_login(self):
        other = make_user(6)
        response = self.admin_client.patch(f"/api/v1/admin/users/{other.pk}/status/", {"approval_status": "disapproved"}, format="json")
        self.assertEqual(response.status_code, 200)
        other.refresh_from_db()
        self.assertEqual((other.approval_status, other.is_active), ("disapproved", True))


class ConcurrentRejectionTests(TransactionTestCase):
    def setUp(self):
        UserLevel.objects.create(code="bronze", name="Bronze", minimum_approved_tasks=0, display_order=1, strike_limit=2)
        self.admin = make_user(0, is_staff=True)
        self.user = make_user(1)

    def _reject(self, claim_id, barrier):
        from django.db import close_old_connections
        close_old_connections()
        try:
            barrier.wait(timeout=5)
            reject_submitted_task(task_id=claim_id, reviewer=User.objects.get(pk=self.admin.pk))
            return True
        except Exception:
            return False
        finally:
            close_old_connections()

    def _race(self, claim_ids):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(len(claim_ids))
        with ThreadPoolExecutor(max_workers=len(claim_ids)) as pool:
            return list(pool.map(lambda claim_id: self._reject(claim_id, barrier), claim_ids))

    def test_concurrent_rejections_of_one_claim_create_one_strike(self):
        claim = submitted_claim(self.user)
        outcomes = self._race([claim.pk, claim.pk, claim.pk])
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(TaskStrike.objects.filter(claim=claim).count(), 1)
        self.user.refresh_from_db()
        self.assertEqual(self.user.approval_status, "approved")

    def test_concurrent_rejections_of_different_claims_are_all_counted(self):
        first, second = submitted_claim(self.user), submitted_claim(self.user)
        outcomes = self._race([first.pk, second.pk])
        self.assertEqual(sum(outcomes), 2)
        self.assertEqual(TaskStrike.objects.filter(user=self.user).count(), 2)
        self.user.refresh_from_db()
        self.assertEqual(self.user.approval_status, "disapproved")
