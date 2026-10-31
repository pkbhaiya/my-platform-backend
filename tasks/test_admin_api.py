from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

import wallet.models
from orders.models import Order
from services_catalog.models import Service
from tasks.models import Task, TaskClaim, TaskStrike

User = get_user_model()


class AdminTaskReviewAPITests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="admin-test@example.com",
            password="TestAdminPassword123!",
            is_staff=True,
            is_active=True,
            approval_status="approved",
        )
        self.user = User.objects.create_user(
            email="regular-test@example.com",
            password="TestUserPassword123!",
            is_staff=False,
            is_active=True,
            approval_status="approved",
        )

        service = Service.objects.create(
            name="Review Test Service",
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
            target_link="https://example.com/test",
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

        self.task = Task.objects.create(
            order=order,
            unit_number=1,
            target_link=order.target_link,
            price_per_1000_snapshot=Decimal("100.0000"),
            user_reward_percentage_snapshot=Decimal("25.00"),
            reward_amount=Decimal("0.02500000"),
            total_slots=5,
            remaining_slots=0,
            status=Task.Status.COMPLETED,
        )
        self.claim = TaskClaim.objects.create(task=self.task, user=self.user, status=TaskClaim.Status.WORK_SUBMITTED, submitted_result="work_done")
        self.url = f"/api/v1/admin/task-submissions/{self.claim.id}/review/"

    def test_anonymous_user_cannot_review(self):
        response = self.client.post(
            self.url,
            {"action": "approve"},
            format="json",
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_401_UNAUTHORIZED,
        )

    def test_regular_user_cannot_review(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.url,
            {"action": "approve"},
            format="json",
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_admin_can_approve_and_credit_once(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(
            self.url,
            {"action": "approve"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(
            wallet.models.WalletTransaction.objects.filter(
                task=self.task,
                claim=self.claim,
                transaction_type="task_reward",
            ).count(),
            1,
        )

    def test_admin_can_reject_and_create_strike(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(
            self.url,
            {"action": "reject"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(
            TaskClaim.objects.get(pk=self.claim.pk).status,
            TaskClaim.Status.REJECTED,
        )
        self.assertEqual(
            TaskStrike.objects.filter(task=self.task).count(),
            1,
        )
        self.assertFalse(
            wallet.models.WalletTransaction.objects.filter(task=self.task).exists()
        )

    def test_review_action_changes_only_the_selected_task(self):
        other_user = User.objects.create_user(email="other-submitter@example.com", password="TestUserPassword123!")
        other_claim = TaskClaim.objects.create(task=self.task, user=other_user, status=TaskClaim.Status.WORK_SUBMITTED)
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(
            self.url,
            {"action": "reject"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(TaskClaim.objects.get(pk=self.claim.pk).status, TaskClaim.Status.REJECTED)
        self.assertEqual(TaskClaim.objects.get(pk=other_claim.pk).status, TaskClaim.Status.WORK_SUBMITTED)

    def test_admin_can_list_pending_tasks(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/v1/admin/tasks/pending/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_pending_tasks_are_paginated(self):
        extra_tasks = [self.task] * 3
        TaskClaim.objects.bulk_create([
            TaskClaim(task=task, user=User.objects.create_user(email=f"pending-{index}@example.com", password="TestUserPassword123!"), status=TaskClaim.Status.WORK_SUBMITTED)
            for index, task in enumerate(extra_tasks)
        ])
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/v1/admin/tasks/pending/?page_size=2")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 4)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertIsNotNone(response.data["next"])
        self.assertEqual(response.data["results"][0]["task_id"], self.task.id)
        self.assertEqual(
            response.data["results"][0]["status"],
            TaskClaim.Status.WORK_SUBMITTED,
        )

    def test_regular_user_cannot_list_pending_tasks(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/v1/admin/tasks/pending/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_view_review_history(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.post(
            self.url,
            {"action": "approve"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get("/api/v1/admin/tasks/review-history/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], self.claim.id)
        self.assertNotIn("review_note", response.data["results"][0])

    def test_review_history_is_paginated(self):
        from django.utils import timezone

        self.claim.status = TaskClaim.Status.APPROVED
        self.claim.reviewed_at = timezone.now()
        self.claim.reviewed_by = self.admin
        self.claim.save()
        extra_tasks = [self.task] * 3
        TaskClaim.objects.bulk_create([
            TaskClaim(task=task, user=User.objects.create_user(email=f"reviewed-{index}@example.com", password="TestUserPassword123!"), status=TaskClaim.Status.REJECTED, reviewed_at=timezone.now(), reviewed_by=self.admin)
            for index, task in enumerate(extra_tasks)
        ])
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/v1/admin/tasks/review-history/?page_size=2")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 4)
        self.assertEqual(len(response.data["results"]), 2)
        self.assertIsNotNone(response.data["next"])

    def test_regular_user_cannot_view_review_history(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/v1/admin/tasks/review-history/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_dashboard_returns_its_sections(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/v1/admin/dashboard/stats/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(set(response.data), {"period", "currency", "financials", "trend", "users", "orders", "tasks", "reconciliation_alert"})
        self.assertEqual(response.data["orders"], {"total": 1, "today": 1, "total_quantity": 1})
        self.assertEqual(response.data["tasks"]["total"], 1)
        self.assertEqual(response.data["tasks"]["available"], 0)
        self.assertEqual(response.data["tasks"]["in_progress_claims"], 0)
        for removed in ("pending_review", "approved", "claimed_task_value_today", "claimed_task_value_by_user"):
            self.assertNotIn(removed, response.data["tasks"])
            self.assertNotIn(removed, response.data["financials"]["current"])
        self.assertNotIn("total_approved_task_rewards", response.data)

    def complete_with_approved_claim(self, **order_fields):
        Order.objects.filter(pk=self.task.order_id).update(fulfillment_status=Order.FulfillmentStatus.COMPLETED, **order_fields)
        TaskClaim.objects.filter(pk=self.claim.pk).update(status=TaskClaim.Status.APPROVED)

    def test_financial_dashboard_uses_recorded_cost_and_credits_once(self):
        self.complete_with_approved_claim(fulfillment_cost_per_1000_snapshot=Decimal("20.0000"), has_fulfillment_cost_snapshot=True)
        test_wallet = wallet.models.Wallet.objects.create(user=self.user, balance=Decimal("0.02500000"))
        wallet.models.WalletTransaction.objects.create(
            wallet=test_wallet,
            transaction_type=wallet.models.WalletTransaction.TransactionType.TASK_REWARD,
            direction=wallet.models.WalletTransaction.Direction.CREDIT,
            amount=Decimal("0.02500000"),
            balance_after=Decimal("0.02500000"),
            task=self.task,
            claim=self.claim,
            description="Test reward ledger entry",
        )
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/v1/admin/dashboard/stats/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        current = response.data["financials"]["current"]
        # Revenue 0.1 - recorded cost 20/1,000 for 1 delivered unit - the one reward credit.
        self.assertEqual(Decimal(current["order_value"]), Decimal("0.1"))
        self.assertEqual(Decimal(current["recognized_revenue"]), Decimal("0.1"))
        self.assertEqual(Decimal(current["paid_to_users"]), Decimal("0.025"))
        self.assertEqual(Decimal(current["profit"]), Decimal("0.055"))

    def test_cancelled_orders_are_not_counted_as_revenue(self):
        Order.objects.filter(pk=self.task.order_id).update(fulfillment_status=Order.FulfillmentStatus.CANCELLED)
        self.client.force_authenticate(user=self.admin)
        current = self.client.get("/api/v1/admin/dashboard/stats/").data["financials"]["current"]
        self.assertEqual(Decimal(current["order_value"]), Decimal("0"))
        self.assertEqual(Decimal(current["recognized_revenue"]), Decimal("0"))

    def test_financial_profit_is_unavailable_when_a_required_cost_is_missing(self):
        # The service has a fulfillment cost, but it was never recorded.
        self.complete_with_approved_claim(has_fulfillment_cost_snapshot=True)
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/v1/admin/dashboard/stats/")
        self.assertIsNone(response.data["financials"]["current"]["profit"])
        self.assertFalse(response.data["financials"]["current"]["profit_available"])

    def test_one_click_review_cannot_be_processed_twice(self):
        self.client.force_authenticate(user=self.admin)
        first = self.client.post(self.url, {"action": "approve"}, format="json")
        second = self.client.post(self.url, {"action": "approve"}, format="json")
        third = self.client.post(self.url, {"action": "reject"}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual((second.status_code, third.status_code), (400, 400))
        self.assertEqual(wallet.models.WalletTransaction.objects.filter(claim=self.claim).count(), 1)
        self.assertFalse(TaskStrike.objects.filter(claim=self.claim).exists())

    def test_review_does_not_require_a_note(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self.url, {"action": "reject"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(TaskClaim.objects.get(pk=self.claim.pk).review_note, "")

    def test_pending_queue_exposes_submitted_result(self):
        self.client.force_authenticate(user=self.admin)
        item = self.client.get("/api/v1/admin/tasks/pending/").data["results"][0]
        self.assertEqual(item["submitted_result"], "work_done")
        self.assertEqual(item["submitted_result_display"], "Work Done")

    def test_admin_orders_include_linked_task_capacity(self):
        self.client.force_authenticate(user=self.admin)
        row = self.client.get("/api/v1/admin/orders/").data["results"][0]
        self.assertEqual(row["task_id"], self.task.pk)
        self.assertEqual((row["total_slots"], row["remaining_slots"]), (5, 0))

    def test_regular_user_cannot_view_dashboard_stats(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/v1/admin/dashboard/stats/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_list_orders(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/v1/admin/orders/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("count", response.data)
        self.assertIn("results", response.data)
        self.assertEqual(response.data["count"], 1)

    def test_regular_user_cannot_list_orders(self):
        self.client.force_authenticate(user=self.user)

        response = self.client.get("/api/v1/admin/orders/")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_can_filter_orders_by_status(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/v1/admin/orders/?fulfillment_status=accepted")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(
            response.data["results"][0]["fulfillment_status"],
            "accepted",
        )

    def test_admin_orders_reject_invalid_status(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(
            "/api/v1/admin/orders/?fulfillment_status=invalid_status"
        )

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_admin_orders_pagination(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/v1/admin/orders/?page_size=1")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(len(response.data["results"]), 1)
        self.assertIn("next", response.data)
        self.assertIn("previous", response.data)

    def test_admin_orders_page_size_is_limited(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/v1/admin/orders/?page_size=500")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertLessEqual(len(response.data["results"]), 100)

    def test_admin_can_filter_orders_by_date(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get(
            "/api/v1/admin/orders/?date_from=2020-01-01&date_to=2099-12-31"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_admin_orders_reject_invalid_date(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/v1/admin/orders/?date_from=invalid-date")

        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_admin_can_search_orders_by_target_link(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/v1/admin/orders/?search=example.com/test")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_admin_search_returns_no_match(self):
        self.client.force_authenticate(user=self.admin)

        response = self.client.get("/api/v1/admin/orders/?search=no-such-order-xyz")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 0)


class AdminTaskStrikeListAPITests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            email="strike-admin@example.com", password="TestAdminPassword123!",
            is_staff=True, is_active=True, approval_status="approved",
        )
        self.user = User.objects.create_user(
            email="strike-user@example.com", password="TestUserPassword123!",
            is_staff=False, is_active=True, approval_status="approved",
        )
        service = Service.objects.create(
            name="Strike Test Service", price_per_1000=Decimal("100.0000"),
            minimum_order_quantity=1, capacity_percentage=Decimal("100.00"),
            business_share_percentage=Decimal("75.00"), user_reward_percentage=Decimal("25.00"),
            order_source="both",
        )
        order = Order.objects.create(
            service=service, source="api", target_link="https://example.com/strike", quantity=1,
            price_per_1000_snapshot=Decimal("100.0000"), business_share_percentage_snapshot=Decimal("75.00"),
            user_reward_percentage_snapshot=Decimal("25.00"), capacity_percentage_snapshot=Decimal("100.00"),
            eligible_user_count_at_acceptance=1, capacity_limit_at_acceptance=1,
            total_order_value=Decimal("0.10000000"),
        )
        self.task = Task.objects.create(
            order=order, unit_number=1, target_link=order.target_link,
            price_per_1000_snapshot=Decimal("100.0000"), user_reward_percentage_snapshot=Decimal("25.00"),
            reward_amount=Decimal("0.02500000"), status=Task.Status.UNAVAILABLE, remaining_slots=0,
        )
        self.claim = TaskClaim.objects.create(task=self.task, user=self.user, status=TaskClaim.Status.REJECTED, submitted_result="work_done", reviewed_at=timezone.now())
        self.strike = TaskStrike.objects.create(
            task=self.task, claim=self.claim, user=self.user, issued_by=self.admin, reason="Invalid result",
        )

    def test_admin_can_filter_task_strikes(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(f"/api/v1/admin/task-strikes/?user_id={self.user.id}&task_id={self.task.id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["id"], self.strike.id)
        self.assertEqual(response.data["results"][0]["reason"], "Invalid result")

    def test_normal_user_cannot_list_task_strikes(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.get("/api/v1/admin/task-strikes/").status_code, 403)

    def test_user_review_status_is_authenticated_and_scoped_to_current_user(self):
        other_user = User.objects.create_user(
            email="other-strike-user@example.com", password="TestUserPassword123!",
            is_staff=False, is_active=True, approval_status="approved",
        )
        other_task = Task.objects.create(
            target_link=self.task.target_link, reward_amount=self.task.reward_amount,
            status=Task.Status.UNAVAILABLE, remaining_slots=0,
        )
        other_claim = TaskClaim.objects.create(task=other_task, user=other_user, status=TaskClaim.Status.REJECTED, submitted_result="work_done", reviewed_at=timezone.now())
        TaskStrike.objects.create(
            task=other_task, claim=other_claim, user=other_user, issued_by=self.admin, reason="Other user only",
        )
        response = self.client.get("/api/v1/tasks/my/review-status/")
        self.assertEqual(response.status_code, 401)
        self.client.force_authenticate(user=self.user)
        response = self.client.get("/api/v1/tasks/my/review-status/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["strike_count"], 1)
        self.assertEqual(response.data["rejected_task_count"], 1)
        self.assertEqual(response.data["latest_rejected_task"]["id"], self.task.pk)


def make_task(title, reward="1", slots=1, target="https://example.com/task"):
    return Task.objects.create(title=title, target_link=target, reward_amount=Decimal(reward), total_slots=slots, remaining_slots=slots)


def wait_after_open(task, user, client):
    from datetime import timedelta
    opened = client.post(f"/api/v1/tasks/{task.pk}/open-target/", {}, format="json")
    assert opened.status_code == 200, opened.data
    TaskClaim.objects.filter(task=task, user=user).update(target_opened_at=timezone.now() - timedelta(seconds=16))


class AdminTaskHistoryAPITests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(email="standalone-admin@example.com", password="TestAdminPassword123!", is_staff=True, is_active=True, approval_status="approved")
        self.user = User.objects.create_user(email="standalone-user@example.com", password="TestUserPassword123!", is_staff=False, is_active=True, approval_status="approved", platform_username="actual_handle")

    def test_standalone_create_task_endpoint_is_removed(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post("/api/v1/admin/tasks/create/", {"title": "x"}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Task.objects.exists())

    def test_wallet_adjustment_endpoint_is_removed_without_touching_ledger(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post("/api/v1/admin/wallet/adjustments/", {"user_id": self.user.pk, "direction": "credit", "amount": "5", "reason": "x"}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(wallet.models.WalletTransaction.objects.exists())

    def test_shared_task_leaves_claimers_list_and_stays_available_to_others(self):
        task = make_task("Shared task", slots=2)
        self.client.force_authenticate(user=self.user)
        claim = self.client.post(f"/api/v1/tasks/{task.pk}/claim/", {}, format="json")
        self.assertEqual(claim.status_code, 200)
        self.assertEqual(claim.data["claim_id"], TaskClaim.objects.get(task=task, user=self.user).pk)
        task.refresh_from_db()
        self.assertEqual(task.remaining_slots, 1)
        self.assertEqual(self.client.get("/api/v1/tasks/my/").data[0]["status"], "claimed")
        wait_after_open(task, self.user, self.client)
        submitted = self.client.post(f"/api/v1/tasks/{task.pk}/submit/", {"result": "work_done", "note": "Done"}, format="json")
        self.assertEqual(submitted.status_code, 200)
        self.assertEqual(self.client.get("/api/v1/tasks/my/").data, [])
        self.assertEqual(self.client.post(f"/api/v1/tasks/{task.pk}/claim/", {}, format="json").status_code, 400)
        self.assertNotIn(task.pk, [item["id"] for item in self.client.get("/api/v1/tasks/available/").data["results"]])
        other_user = User.objects.create_user(email="second-claimant@example.com", password="TestUserPassword123!", approval_status="approved", is_active=True)
        self.client.force_authenticate(user=other_user)
        self.assertIn(task.pk, [item["id"] for item in self.client.get("/api/v1/tasks/available/").data["results"]])

    def test_non_admin_cannot_access_task_submission_details(self):
        task = Task.objects.create(target_link="https://example.com/private-history")
        self.client.force_authenticate(user=self.user)
        response = self.client.get(f"/api/v1/admin/tasks/{task.pk}/submissions/")
        self.assertEqual(response.status_code, 403)

    def test_non_admin_cannot_access_task_history(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.get("/api/v1/admin/tasks/history/").status_code, 403)

    def test_task_history_filters_and_is_separate_from_pending_review_queue(self):
        task = make_task("History record", reward="1.25")
        self.client.force_authenticate(user=self.admin)
        history = self.client.get("/api/v1/admin/tasks/history/?status=available&search=History")
        pending = self.client.get("/api/v1/admin/tasks/pending/")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["count"], 1)
        row = history.data["results"][0]
        self.assertEqual(row["id"], task.pk)
        self.assertEqual((row["total_slots"], row["remaining_slots"]), (1, 1))
        self.assertEqual(row["total_submissions"], 0)
        self.assertEqual(pending.data["count"], 0)

    def test_history_rows_are_unique_and_each_tasks_claims_stay_with_that_task(self):
        pending = make_task("Shared URL first", slots=3, target="https://example.com/shared")
        approved = make_task("Shared URL second", reward="2", slots=3, target="https://example.com/shared")
        other = User.objects.create_user(email="history-other@example.com", password="TestUserPassword123!", platform_username="someone_else")
        TaskClaim.objects.create(task=pending, user=self.user, status=TaskClaim.Status.WORK_SUBMITTED, submitted_result="work_done", submitted_at=timezone.now())
        TaskClaim.objects.create(task=pending, user=other, status=TaskClaim.Status.APPROVED, submitted_result="work_done", submitted_at=timezone.now(), reviewed_at=timezone.now())
        TaskClaim.objects.create(task=approved, user=self.user, status=TaskClaim.Status.APPROVED, submitted_result="work_done", submitted_at=timezone.now(), reviewed_at=timezone.now())
        self.client.force_authenticate(user=self.admin)
        response = self.client.get("/api/v1/admin/tasks/history/")
        self.assertEqual(response.data["count"], 2)
        self.assertEqual([item["id"] for item in response.data["results"]], [pending.pk, approved.pk])
        filtered = self.client.get("/api/v1/admin/tasks/history/?username=actual_handle")
        self.assertEqual(filtered.data["count"], 2)
        by_id = {item["id"]: item for item in filtered.data["results"]}
        self.assertEqual(by_id[pending.pk]["total_claims"], 2)
        self.assertEqual(by_id[pending.pk]["pending_reviews"], 1)
        self.assertEqual(by_id[approved.pk]["approved_submissions"], 1)
        submissions = self.client.get(f"/api/v1/admin/tasks/{pending.pk}/submissions/").data["results"]
        self.assertEqual({row["task_id"] for row in submissions}, {pending.pk})
        self.assertEqual([row["platform_username"] for row in submissions], ["actual_handle", "someone_else"])
        self.assertEqual(submissions[0]["submitted_result_display"], "Work Done")
        self.assertEqual(submissions[1]["status_display"], "Approved")
        self.assertNotIn("review_note", submissions[0])
        self.assertEqual(self.client.get("/api/v1/admin/tasks/999999/submissions/").status_code, 404)

    def test_task_history_is_paginated_and_rejects_invalid_status(self):
        for index in range(3):
            make_task(f"History {index}", target=f"https://example.com/{index}")
        self.client.force_authenticate(user=self.admin)
        page = self.client.get("/api/v1/admin/tasks/history/?page_size=2")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.data["count"], 3)
        self.assertEqual(len(page.data["results"]), 2)
        invalid = self.client.get("/api/v1/admin/tasks/history/?status=not_real")
        self.assertEqual(invalid.status_code, 400)

    def test_claim_submit_and_one_click_approval_credit_the_reward_once(self):
        task = make_task("Direct review task", reward="2.5")
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.post(f"/api/v1/tasks/{task.pk}/claim/", {}, format="json").status_code, 200)
        wait_after_open(task, self.user, self.client)
        submitted = self.client.post(f"/api/v1/tasks/{task.pk}/submit/", {"result": "work_done"}, format="json")
        self.assertEqual(submitted.status_code, 200)
        claim = TaskClaim.objects.get(task=task, user=self.user)
        self.client.force_authenticate(user=self.admin)
        reviewed = self.client.post(f"/api/v1/admin/task-submissions/{claim.pk}/review/", {"action": "approve"}, format="json")
        self.assertEqual(reviewed.status_code, 200)
        self.assertEqual(reviewed.data["status"], "approved")
        transaction = wallet.models.WalletTransaction.objects.get(task_id=task.pk)
        self.assertEqual(transaction.amount, Decimal("2.50000000"))
