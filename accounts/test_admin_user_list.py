"""Phase 11 (revised), part C: a lean, fast admin Users list; the rest is in the user detail view."""

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from accounts.identity import record_signal
from accounts.models import PlatformUsername, User, UserLevel
from tasks.models import Task, TaskClaim, TaskStrike
from tasks.test_claim_lifecycle import make_user
from wallet.models import Wallet, WalletTransaction

LIST = "/api/v1/admin/users/"
COLUMNS = {"id", "username", "level", "approval_status", "approval_status_display", "strikes", "joined_on",
           "is_staff", "is_superuser", "is_active"}


class AdminUserListTests(TestCase):
    def setUp(self):
        self.admin = make_user(0, is_staff=True)
        self.api = APIClient()
        self.api.force_authenticate(self.admin)
        UserLevel.objects.create(code="bronze", name="Bronze", minimum_approved_tasks=0, display_order=1, strike_limit=5)
        UserLevel.objects.create(code="silver", name="Silver", minimum_approved_tasks=2, display_order=2, strike_limit=4)
        self.task = Task.objects.create(target_link="https://example.com/t", reward_amount=Decimal("1"), total_slots=500, remaining_slots=500)

    def make_workers(self, count, start=1):
        users = []
        for index in range(start, start + count):
            user = make_user(index)
            approved = TaskClaim.objects.create(task=self.task, user=user, status=TaskClaim.Status.APPROVED)
            rejected = TaskClaim.objects.create(task=self.task, user=user, status=TaskClaim.Status.REJECTED)
            TaskStrike.objects.create(task=self.task, claim=rejected, user=user, issued_by=self.admin, reason="")
            users.append((user, approved))
        return users

    def page_queries(self, **params):
        with CaptureQueriesContext(connection) as queries:
            response = self.api.get(LIST, params)
        self.assertEqual(response.status_code, 200)
        return len(queries), response.data

    def test_rows_have_only_the_visible_columns(self):
        (user, _claim), = self.make_workers(1)
        TaskClaim.objects.create(task=Task.objects.create(target_link="https://example.com/u", reward_amount=Decimal("1")),
                                 user=user, status=TaskClaim.Status.APPROVED)
        row = next(row for row in self.api.get(LIST).data["results"] if row["id"] == user.pk)
        self.assertEqual(set(row), COLUMNS)
        self.assertEqual((row["username"], row["approval_status"], row["approval_status_display"]),
                         (user.platform_username, "approved", "Approved"))
        self.assertEqual((row["level"]["code"], row["strikes"]), ("silver", {"count": 1, "limit": 4, "reached": False}))
        for hidden in ("email", "whatsapp_number", "state", "gender", "referral_code", "referred_by", "linked_accounts"):
            self.assertNotIn(hidden, row)

    def test_joined_is_the_ist_calendar_day_and_the_detail_has_the_full_time(self):
        late = make_user(50)
        User.objects.filter(pk=late.pk).update(date_joined=datetime(2026, 10, 25, 20, 0, tzinfo=dt_timezone.utc))  # 26 Oct, 01:30 IST
        row = next(row for row in self.api.get(LIST).data["results"] if row["id"] == late.pk)
        self.assertEqual(row["joined_on"], "2026-10-26")
        self.assertEqual(self.api.get(f"{LIST}{late.pk}/").data["date_joined"], "2026-10-25T20:00:00+00:00")

    def test_a_page_costs_the_same_few_queries_however_many_users_it_shows(self):
        self.make_workers(2)
        small, data = self.page_queries()
        self.assertEqual(data["count"], 3)
        self.make_workers(30, start=10)
        large, data = self.page_queries()
        self.assertEqual((data["count"], len(data["results"])), (33, 20))
        self.assertEqual(small, large)
        self.assertLessEqual(large, 5)
        filtered, _data = self.page_queries(search="life", approval_status="approved", level="silver", state="DL", has_linked_accounts="false")
        self.assertLessEqual(filtered, 5)

    def test_pagination_search_and_filters(self):
        workers = [user for user, _claim in self.make_workers(25)]
        first, second = self.api.get(LIST).data, self.api.get(LIST, {"page": 2}).data
        self.assertEqual((first["count"], len(first["results"]), len(second["results"])), (26, 20, 6))
        target = workers[3]
        for term in (str(target.pk), target.platform_username.upper(), target.email, target.referral_code.lower()):
            self.assertIn(target.pk, [row["id"] for row in self.api.get(LIST, {"search": term}).data["results"]], term)
        User.objects.filter(pk=workers[0].pk).update(approval_status="pending", state="KA")
        self.assertEqual([row["id"] for row in self.api.get(LIST, {"approval_status": "pending"}).data["results"]], [workers[0].pk])
        self.assertEqual([row["id"] for row in self.api.get(LIST, {"state": "KA"}).data["results"]], [workers[0].pk])
        TaskClaim.objects.filter(user=workers[1], status=TaskClaim.Status.APPROVED).delete()  # 0 approved: Bronze
        TaskClaim.objects.create(task=Task.objects.create(target_link="https://example.com/s", reward_amount=Decimal("1")),
                                 user=workers[2], status=TaskClaim.Status.APPROVED)  # 2 approved: Silver
        self.assertEqual([row["id"] for row in self.api.get(LIST, {"level": "silver"}).data["results"]], [workers[2].pk])
        bronze = [row["id"] for row in self.api.get(LIST, {"level": "bronze", "page_size": 100}).data["results"]]
        self.assertIn(workers[1].pk, bronze)
        self.assertNotIn(workers[2].pk, bronze)
        record_signal(workers[5], "device_id", "device", "11111111-1111-4111-8111-111111111111")
        record_signal(workers[6], "device_id", "device", "11111111-1111-4111-8111-111111111111")
        linked = {row["id"] for row in self.api.get(LIST, {"has_linked_accounts": "true"}).data["results"]}
        self.assertEqual(linked, {workers[5].pk, workers[6].pk})

    def test_the_detail_view_has_everything_the_list_leaves_out(self):
        (user, claim), = self.make_workers(1)
        PlatformUsername.objects.create(user=user, platform_name="YouTube", username="life_tube")
        wallet = Wallet.objects.create(user=user, balance=Decimal("12.50"))
        WalletTransaction.objects.create(wallet=wallet, transaction_type="task_reward", direction="credit", amount=Decimal("12.50"),
                                         balance_after=Decimal("12.50"), task=self.task, claim=claim, reward_claim=claim)
        detail = self.api.get(f"{LIST}{user.pk}/").data
        for field in ("email", "whatsapp_number", "state_display", "gender_display", "referral_code", "referred_by",
                      "linked_accounts", "target_not_available_rate", "date_joined", "password_changes"):
            self.assertIn(field, detail)
        self.assertEqual(detail["platform_usernames"][0]["username"], "life_tube")
        self.assertEqual((detail["earnings"]["wallet_balance"], detail["earnings"]["task_rewards"], detail["earnings"]["withdrawn"]),
                         ("12.50000000", "12.50000000", "0.00000000"))
        self.assertEqual((detail["is_staff"], detail["is_superuser"]), (False, False))
