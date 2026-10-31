from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from orders.models import Order
from services_catalog.models import Service
from tasks.models import Task, TaskClaim, TaskUserClaim
from tasks.services import create_tasks_for_order
from wallet.models import Wallet, WalletTransaction, WithdrawalRequest

User = get_user_model()


def make_user(index, **extra):
    fields = dict(
        email=f"worker-{index}@example.com", password="TestPassword!9317",
        full_name=f"Worker {index}", platform_username=f"worker_{index}",
        whatsapp_number=f"95678{index:05d}", state="DL", gender="female",
        approval_status="approved", is_active=True,
    )
    fields.update(extra)
    return User.objects.create_user(**fields)


def client_for(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def make_service(name="Instagram Likes", description="Like the linked post."):
    return Service.objects.create(
        name=name, description=description, price_per_1000=Decimal("400.0000"),
        minimum_order_quantity=1, capacity_percentage=Decimal("100.00"), eligible_level="all",
        business_share_percentage=Decimal("75.00"), user_reward_percentage=Decimal("25.00"),
        order_source="both", target_link_required=True,
    )


def make_order(service, quantity):
    return Order.objects.create(
        service=service, source="api", target_link="https://example.com/post", quantity=quantity,
        price_per_1000_snapshot=Decimal("400.0000"), business_share_percentage_snapshot=Decimal("75.00"),
        user_reward_percentage_snapshot=Decimal("25.00"), capacity_percentage_snapshot=Decimal("100.00"),
        eligible_user_count_at_acceptance=100, capacity_limit_at_acceptance=100,
        total_order_value=Decimal("400.0000") * quantity / 1000,
        fulfillment_status=Order.FulfillmentStatus.ACCEPTED,
    )


def finish_wait(task, user):
    TaskClaim.objects.filter(task=task, user=user, status=TaskClaim.Status.CLAIMED).update(
        target_opened_at=timezone.now() - timedelta(minutes=5), target_wait_seconds=15)


class CapacityFlowAPITests(APITestCase):
    """One Order -> one Task with N slots, enforced end to end through the public APIs."""

    def setUp(self):
        self.admin = make_user(0, is_staff=True)
        self.workers = [make_user(index) for index in range(1, 5)]
        self.order = make_order(make_service(), quantity=3)
        create_tasks_for_order(self.order.pk)
        self.task = Task.objects.get(order=self.order)

    def test_quantity_creates_one_task_and_distinct_claims_exhaust_it_exactly(self):
        self.assertEqual(Task.objects.filter(order=self.order).count(), 1)
        self.assertEqual((self.task.total_slots, self.task.remaining_slots), (3, 3))
        for expected_remaining, worker in zip((2, 1, 0), self.workers[:3]):
            response = client_for(worker).post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(Task.objects.get(pk=self.task.pk).remaining_slots, expected_remaining)
        self.task.refresh_from_db()
        self.order.refresh_from_db()
        # All slots held but none approved: fully claimed, not completed.
        self.assertEqual(self.task.status, Task.Status.FULLY_CLAIMED)
        self.assertEqual(self.order.fulfillment_status, Order.FulfillmentStatus.IN_PROGRESS)
        self.assertEqual(self.order.availability_status, Order.AvailabilityStatus.FULLY_CLAIMED)
        late = client_for(self.workers[3]).post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json")
        self.assertEqual(late.status_code, 400)
        self.assertEqual(TaskClaim.objects.filter(task=self.task).count(), 3)
        self.assertEqual(TaskUserClaim.objects.filter(task=self.task).count(), 3)

    def test_duplicate_claim_is_refused_and_does_not_consume_capacity(self):
        worker = client_for(self.workers[0])
        self.assertEqual(worker.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json").status_code, 200)
        again = worker.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json")
        self.assertEqual(again.status_code, 400)
        self.assertEqual(Task.objects.get(pk=self.task.pk).remaining_slots, 2)
        self.assertEqual(TaskClaim.objects.filter(task=self.task, user=self.workers[0]).count(), 1)

    def test_each_participant_keeps_separate_submission_review_and_exact_reward(self):
        reward = Task.objects.get(pk=self.task.pk).reward_amount
        self.assertEqual(reward, Decimal("0.10000000"))
        for worker in self.workers[:2]:
            client = client_for(worker)
            client.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json")
            finish_wait(self.task, worker)
            self.assertEqual(client.post(f"/api/v1/tasks/{self.task.pk}/submit/", {"result": "work_done"}, format="json").status_code, 200)
        approve, reject = TaskClaim.objects.filter(task=self.task).order_by("id")
        admin = client_for(self.admin)
        self.assertEqual(admin.post(f"/api/v1/admin/task-submissions/{approve.pk}/review/", {"action": "approve"}, format="json").status_code, 200)
        self.assertEqual(admin.post(f"/api/v1/admin/task-submissions/{reject.pk}/review/", {"action": "reject"}, format="json").status_code, 200)
        self.assertEqual(Wallet.objects.get(user=self.workers[0]).balance, reward)
        self.assertFalse(WalletTransaction.objects.filter(wallet__user=self.workers[1]).exists())
        # The approval keeps its slot; the rejection gave its slot back.
        self.assertEqual(Task.objects.get(pk=self.task.pk).remaining_slots, 2)


class WorkerSummaryTests(APITestCase):
    def setUp(self):
        self.worker = make_user(1)
        self.other = make_user(2)
        self.client = client_for(self.worker)
        now = timezone.now()
        statuses = ["approved", "approved", "rejected", "work_submitted", "post_not_available", "claimed"]
        for index, claim_status in enumerate(statuses):
            task = Task.objects.create(target_link=f"https://example.com/{index}", reward_amount=Decimal("0.75"))
            TaskClaim.objects.create(task=task, user=self.worker, status=claim_status,
                submitted_at=None if claim_status == "claimed" else now)
        other_task = Task.objects.create(target_link="https://example.com/other", reward_amount=Decimal("9"))
        TaskClaim.objects.create(task=other_task, user=self.other, status="approved", submitted_at=now)
        for user, amounts in ((self.worker, ("0.75", "0.75")), (self.other, ("9",))):
            wallet = Wallet.objects.create(user=user, balance=sum(Decimal(a) for a in amounts))
            running = Decimal("0")
            for claim, amount in zip(TaskClaim.objects.filter(user=user, status="approved").order_by("id"), amounts):
                running += Decimal(amount)
                WalletTransaction.objects.create(wallet=wallet, task=claim.task, claim=claim, transaction_type="task_reward",
                    direction="credit", amount=Decimal(amount), balance_after=running, description="Task reward")

    def test_summary_counts_and_earnings_cover_only_the_signed_in_user(self):
        response = self.client.get("/api/v1/tasks/my/summary/")
        self.assertEqual(response.status_code, 200)
        data = response.data
        self.assertEqual((data["approved"], data["rejected"], data["pending_review"], data["in_progress"]), (2, 1, 1, 1))
        self.assertEqual(data["total_claims"], 6)
        self.assertEqual(data["claims_by_status"]["post_not_available"], 1)
        self.assertEqual(data["total_earned"], "1.50000000")
        self.assertEqual(data["currency"], "INR")

    def test_summary_requires_an_approved_signed_in_user(self):
        self.assertEqual(APIClient().get("/api/v1/tasks/my/summary/").status_code, 401)
        for approval_status in ("pending", "disapproved"):
            User.objects.filter(pk=self.worker.pk).update(approval_status=approval_status)
            self.assertEqual(client_for(User.objects.get(pk=self.worker.pk)).get("/api/v1/tasks/my/summary/").status_code, 403)

    def test_history_status_filter_returns_only_own_matching_claims(self):
        approved = self.client.get("/api/v1/tasks/history/?status=approved")
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.data["count"], 2)
        self.assertTrue(all(row["status"] == "approved" for row in approved.data["results"]))
        self.assertEqual(self.client.get("/api/v1/tasks/history/").data["count"], 5)
        row = approved.data["results"][0]
        self.assertIn("reviewed_at", row)
        self.assertIn("submitted_result_display", row)
        for invalid in ("claimed", "nonsense"):
            self.assertEqual(self.client.get(f"/api/v1/tasks/history/?status={invalid}").status_code, 400)


class WorkerListFilterTests(APITestCase):
    def setUp(self):
        self.worker = make_user(1)
        self.client = client_for(self.worker)

    def test_available_search_matches_service_name_and_description_only(self):
        likes = make_order(make_service("Instagram Likes", "Like the post."), 2)
        follows = make_order(make_service("YouTube Subscribers", "Subscribe to the channel."), 2)
        for order in (likes, follows):
            create_tasks_for_order(order.pk)
        Task.objects.filter(order=follows).update(target_link="https://instagram.example/secret")

        def ids(query):
            response = self.client.get("/api/v1/tasks/available/", {"search": query})
            self.assertEqual(response.status_code, 200)
            return [row["id"] for row in response.data["results"]]

        likes_task = Task.objects.get(order=likes).pk
        follows_task = Task.objects.get(order=follows).pk
        self.assertEqual(ids("instagram"), [likes_task])
        self.assertEqual(ids("channel"), [follows_task])
        self.assertEqual(ids("secret"), [])
        self.assertEqual(sorted(ids("")), sorted([likes_task, follows_task]))

    def test_wallet_transaction_and_withdrawal_filters_validate_input(self):
        wallet = Wallet.objects.create(user=self.worker, balance=Decimal("10"))
        WalletTransaction.objects.create(wallet=wallet, transaction_type="adjustment", direction="credit",
            amount=Decimal("10"), balance_after=Decimal("10"), description="Opening balance")
        WithdrawalRequest.objects.create(user=self.worker, amount=Decimal("5"), payout_method="upi", payout_details="w@upi")
        self.assertEqual(self.client.get("/api/v1/wallet/transactions/?transaction_type=adjustment").data["count"], 1)
        self.assertEqual(self.client.get("/api/v1/wallet/transactions/?transaction_type=task_reward").data["count"], 0)
        self.assertEqual(self.client.get("/api/v1/wallet/transactions/?transaction_type=bogus").status_code, 400)
        self.assertEqual(self.client.get("/api/v1/wallet/withdrawals/?status=pending").data["count"], 1)
        self.assertEqual(self.client.get("/api/v1/wallet/withdrawals/?status=approved").data["count"], 0)
        self.assertEqual(self.client.get("/api/v1/wallet/withdrawals/?status=bogus").status_code, 400)


class SubmittedResultTests(APITestCase):
    def setUp(self):
        self.worker = make_user(1)
        self.admin = make_user(0, is_staff=True)
        self.client = client_for(self.worker)
        self.task = Task.objects.create(target_link="https://example.com/result", total_slots=2, remaining_slots=2, reward_amount=Decimal("1"))

    def test_api_submission_stores_the_chosen_result(self):
        self.client.post(f"/api/v1/tasks/{self.task.pk}/claim/", {}, format="json")
        finish_wait(self.task, self.worker)
        self.client.post(f"/api/v1/tasks/{self.task.pk}/submit/", {"result": "post_not_available"}, format="json")
        claim = TaskClaim.objects.get(task=self.task, user=self.worker)
        self.assertEqual((claim.status, claim.submitted_result), ("post_not_available", "post_not_available"))
        row = client_for(self.admin).get(f"/api/v1/admin/tasks/{self.task.pk}/submissions/").data["results"][0]
        self.assertEqual(row["submitted_result_display"], "Target Not Available")

    def test_older_submissions_without_a_stored_result_are_labelled_from_their_status_without_rewriting(self):
        legacy = TaskClaim.objects.create(task=self.task, user=self.worker, status="approved", submitted_at=timezone.now())
        history = self.client.get("/api/v1/tasks/history/").data["results"][0]
        self.assertEqual(history["submitted_result_display"], "Work Done")
        self.assertIsNone(history["submitted_result"])
        admin_row = client_for(self.admin).get(f"/api/v1/admin/tasks/{self.task.pk}/submissions/").data["results"][0]
        self.assertEqual(admin_row["submitted_result_display"], "Work Done")
        legacy.refresh_from_db()
        self.assertIsNone(legacy.submitted_result)
