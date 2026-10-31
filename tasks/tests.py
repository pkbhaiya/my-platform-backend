from decimal import Decimal

from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase, SimpleTestCase, TransactionTestCase
from unittest.mock import patch

from orders.models import Order
from services_catalog.models import Service
from accounts.models import UserLevel
from tasks.models import Task, TaskClaim
from tasks.calculations import calculate_task_reward
from tasks.services import (
    approve_submitted_task,
    claim_task,
    create_tasks_for_order,
    reject_submitted_task,
    submit_task_result,
)


class TaskRewardCalculationTests(SimpleTestCase):

    def test_standard_reward(self):
        self.assertEqual(
            calculate_task_reward("100", "25"),
            Decimal("0.02500000"),
        )

    def test_zero_reward(self):
        self.assertEqual(
            calculate_task_reward("100", "0"),
            Decimal("0E-8"),
        )

    def test_invalid_values(self):
        with self.assertRaises(ValueError):
            calculate_task_reward("-100", "25")

        with self.assertRaises(ValueError):
            calculate_task_reward("100", "101")


def make_user(index, **extra):
    fields = dict(
        email=f"task-user-{index}@example.com", password="TestPassword!9317",
        full_name=f"Task User {index}", platform_username=f"task_user_{index}",
        whatsapp_number=f"98765{index:05d}", state="DL", gender="female",
        approval_status="approved", is_active=True,
    )
    fields.update(extra)
    return get_user_model().objects.create_user(**fields)


def open_and_wait(task, user, seconds=16):
    """Record the target-open event, then move it into the past to simulate the wait."""
    from datetime import timedelta
    from django.utils import timezone
    from tasks.services import open_task_target
    open_task_target(task_id=task.pk, user=user)
    TaskClaim.objects.filter(task=task, user=user, status=TaskClaim.Status.CLAIMED).update(
        target_opened_at=timezone.now() - timedelta(seconds=seconds))


class TaskCreationServiceTests(TestCase):

    def setUp(self):
        self.service = Service.objects.create(
            name="Test Likes Service",
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
        self.order = self.make_order(3)
        self.user = make_user(1)
        self.admin = make_user(99, is_staff=True, approval_status="approved")

    def make_order(self, quantity):
        return Order.objects.create(
            service=self.service,
            source="api",
            target_link="https://example.com/post",
            quantity=quantity,
            price_per_1000_snapshot=Decimal("100.0000"),
            business_share_percentage_snapshot=Decimal("75.00"),
            user_reward_percentage_snapshot=Decimal("25.00"),
            capacity_percentage_snapshot=Decimal("100.00"),
            eligible_user_count_at_acceptance=100,
            capacity_limit_at_acceptance=100,
            total_order_value=Decimal("100.0000") * quantity / 1000,
            fulfillment_status=Order.FulfillmentStatus.ACCEPTED,
        )

    def task(self):
        create_tasks_for_order(self.order.pk)
        return Task.objects.get(order=self.order)

    def test_quantity_1_10_and_100_each_create_one_task_with_matching_capacity(self):
        for quantity in (1, 10, 100):
            order = self.make_order(quantity)
            self.assertEqual(create_tasks_for_order(order.pk), 1)
            tasks = Task.objects.filter(order=order)
            self.assertEqual(tasks.count(), 1)
            self.assertEqual((tasks[0].total_slots, tasks[0].remaining_slots), (quantity, quantity))
            self.assertEqual(tasks[0].status, Task.Status.AVAILABLE)

    def test_retry_does_not_create_duplicate_tasks_or_capacity(self):
        create_tasks_for_order(self.order.pk)
        create_tasks_for_order(self.order.pk)
        tasks = Task.objects.filter(order=self.order)
        self.assertEqual(tasks.count(), 1)
        self.assertEqual(tasks[0].total_slots, 3)

    def test_task_value_and_reward_are_distinct_and_no_reward_is_precreated(self):
        from wallet.models import WalletTransaction
        task = self.task()
        # Reward per approved claim = price/1000 x reward% (0.1 x 25%).
        self.assertEqual(task.reward_amount, Decimal("0.02500000"))
        # Allocated order value per slot = order value / capacity (0.3 / 3).
        self.assertEqual(self.order.total_order_value / task.total_slots, Decimal("0.1"))
        self.assertFalse(WalletTransaction.objects.exists())

    def test_claim_decrements_by_exactly_one_and_same_user_cannot_reclaim(self):
        task = self.task()
        claimed = claim_task(task_id=task.pk, user=self.user)
        task.refresh_from_db()
        self.assertEqual(task.remaining_slots, 2)
        self.assertEqual(task.status, Task.Status.AVAILABLE)
        with self.assertRaises(ValidationError):
            claim_task(task_id=task.pk, user=self.user)
        task.refresh_from_db()
        self.assertEqual(task.remaining_slots, 2)
        self.assertEqual(claimed.claim_id, TaskClaim.objects.get(task=task).pk)
        self.order.refresh_from_db()
        self.assertEqual(self.order.fulfillment_status, Order.FulfillmentStatus.IN_PROGRESS)

    def test_distinct_users_fill_capacity_then_task_is_fully_claimed_not_completed(self):
        task = self.task()
        users = [self.user, make_user(2), make_user(3)]
        for expected_remaining, user in zip((2, 1, 0), users):
            claim_task(task_id=task.pk, user=user)
            task.refresh_from_db()
            self.assertEqual(task.remaining_slots, expected_remaining)
        # Every slot is held, but nothing is approved yet.
        self.assertEqual(task.status, Task.Status.FULLY_CLAIMED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.fulfillment_status, Order.FulfillmentStatus.IN_PROGRESS)
        self.assertEqual(self.order.availability_status, Order.AvailabilityStatus.FULLY_CLAIMED)
        with self.assertRaises(ValidationError):
            claim_task(task_id=task.pk, user=make_user(4))
        self.assertEqual(TaskClaim.objects.filter(task=task).count(), 3)

    def test_pending_submission_stays_reviewable_after_capacity_is_exhausted(self):
        self.order = self.make_order(1)
        task = self.task()
        claim_task(task_id=task.pk, user=self.user)
        open_and_wait(task, self.user)
        submit_task_result(task_id=task.pk, user=self.user, result="work_done")
        task.refresh_from_db()
        self.assertEqual(task.status, Task.Status.FULLY_CLAIMED)
        claim = TaskClaim.objects.get(task=task)
        self.assertEqual(claim.status, TaskClaim.Status.WORK_SUBMITTED)
        approve_submitted_task(task_id=claim.pk, reviewer=self.admin)
        self.assertEqual(TaskClaim.objects.get(pk=claim.pk).status, TaskClaim.Status.APPROVED)
        # Approving the last held slot completes the Task and its Order.
        task.refresh_from_db()
        self.order.refresh_from_db()
        self.assertEqual(task.status, Task.Status.COMPLETED)
        self.assertEqual(self.order.fulfillment_status, Order.FulfillmentStatus.COMPLETED)

    def test_rejected_and_target_not_available_release_slots_but_never_allow_reclaim(self):
        task = self.task()
        second, third = make_user(2), make_user(3)
        for user in (self.user, second, third):
            claim_task(task_id=task.pk, user=user)
            open_and_wait(task, user)
        submit_task_result(task_id=task.pk, user=self.user, result="work_done")
        submit_task_result(task_id=task.pk, user=second, result="work_done")
        submit_task_result(task_id=task.pk, user=third, result="post_not_available")
        reject_submitted_task(task_id=TaskClaim.objects.get(task=task, user=second).pk, reviewer=self.admin)
        task.refresh_from_db()
        # Only the work_submitted claim still holds a slot.
        self.assertEqual(task.remaining_slots, 2)
        self.assertEqual(task.status, Task.Status.AVAILABLE)
        for user in (self.user, second, third):
            with self.assertRaises(ValidationError):
                claim_task(task_id=task.pk, user=user)

    def test_same_user_cannot_reclaim_after_approval(self):
        task = self.task()
        claim_task(task_id=task.pk, user=self.user)
        open_and_wait(task, self.user)
        submit_task_result(task_id=task.pk, user=self.user, result="work_done")
        approve_submitted_task(task_id=TaskClaim.objects.get(task=task).pk, reviewer=self.admin)
        with self.assertRaises(ValidationError):
            claim_task(task_id=task.pk, user=self.user)
        task.refresh_from_db()
        self.assertEqual(task.remaining_slots, 2)

    def test_reward_and_strike_are_issued_at_most_once(self):
        from wallet.models import WalletTransaction
        from tasks.models import TaskStrike
        task = self.task()
        second = make_user(2)
        for user in (self.user, second):
            claim_task(task_id=task.pk, user=user)
            open_and_wait(task, user)
            submit_task_result(task_id=task.pk, user=user, result="work_done")
        approved = TaskClaim.objects.get(task=task, user=self.user)
        rejected = TaskClaim.objects.get(task=task, user=second)
        approve_submitted_task(task_id=approved.pk, reviewer=self.admin)
        reject_submitted_task(task_id=rejected.pk, reviewer=self.admin)
        with self.assertRaises(ValidationError):
            approve_submitted_task(task_id=approved.pk, reviewer=self.admin)
        with self.assertRaises(ValidationError):
            reject_submitted_task(task_id=rejected.pk, reviewer=self.admin)
        with self.assertRaises(ValidationError):
            approve_submitted_task(task_id=rejected.pk, reviewer=self.admin)
        self.assertEqual(WalletTransaction.objects.filter(claim=approved).count(), 1)
        self.assertEqual(WalletTransaction.objects.filter(claim=approved).get().amount, task.reward_amount)
        self.assertFalse(WalletTransaction.objects.filter(claim=rejected).exists())
        self.assertEqual(TaskStrike.objects.filter(claim=rejected).count(), 1)

    def test_exhausted_task_rejects_claim_without_creating_claim(self):
        task = self.task()
        Task.objects.filter(pk=task.pk).update(remaining_slots=0, status=Task.Status.UNAVAILABLE)
        with self.assertRaises(ValidationError):
            claim_task(task_id=task.pk, user=self.user)
        self.assertFalse(TaskClaim.objects.filter(task=task).exists())

    def test_failed_claim_creation_rolls_back_slot_decrement(self):
        task = self.task()
        with patch("tasks.services.TaskClaim.objects.create", side_effect=IntegrityError("test failure")):
            with self.assertRaises(ValidationError):
                claim_task(task_id=task.pk, user=self.user)
        task.refresh_from_db()
        self.assertEqual(task.remaining_slots, 3)
        self.assertFalse(TaskClaim.objects.filter(task=task).exists())

    def test_rejects_non_accepted_order(self):
        self.order.fulfillment_status = Order.FulfillmentStatus.CANCELLED
        self.order.save()
        with self.assertRaises(ValidationError):
            create_tasks_for_order(self.order.pk)

    def test_level_restriction_uses_only_approved_tasks_and_allows_lower_levels_cumulatively(self):
        UserLevel.objects.create(code="bronze", name="Bronze", minimum_approved_tasks=0, display_order=1)
        UserLevel.objects.create(code="silver", name="Silver", minimum_approved_tasks=1, display_order=2)
        self.service.eligible_level = "silver"
        self.service.save()
        task = self.task()

        TaskClaim.objects.create(task=Task.objects.create(target_link="https://example.com/pending"), user=self.user, status=TaskClaim.Status.WORK_SUBMITTED)
        with self.assertRaises(ValidationError):
            claim_task(task_id=task.pk, user=self.user)

        completed_task = Task.objects.create(target_link="https://example.com/approved")
        TaskClaim.objects.create(task=completed_task, user=self.user, status=TaskClaim.Status.APPROVED)
        claim_task(task_id=task.pk, user=self.user)


class TargetOpenTimerTests(TestCase):
    """The 15-second rule is enforced from the claim's persisted server timestamp."""

    def setUp(self):
        self.user = make_user(1)
        self.other = make_user(2)
        self.task = Task.objects.create(target_link="https://example.com/target", total_slots=2, remaining_slots=2, reward_amount=Decimal("1"))
        claim_task(task_id=self.task.pk, user=self.user)

    def submit(self, user=None, result="work_done", task=None):
        return submit_task_result(task_id=(task or self.task).pk, user=user or self.user, result=result)

    def test_submission_is_rejected_before_the_target_is_opened(self):
        for result in ("work_done", "post_not_available"):
            with self.assertRaisesMessage(ValidationError, "Open the target"):
                self.submit(result=result)

    def test_submission_is_rejected_within_fifteen_seconds_of_opening(self):
        from tasks.services import open_task_target
        opened = open_task_target(task_id=self.task.pk, user=self.user)
        self.assertFalse(opened["submission_eligible"])
        open_and_wait(self.task, self.user, seconds=14)
        with self.assertRaisesMessage(ValidationError, "15 seconds"):
            self.submit()
        self.assertEqual(TaskClaim.objects.get(task=self.task, user=self.user).status, TaskClaim.Status.CLAIMED)

    def test_submission_is_accepted_after_fifteen_seconds(self):
        open_and_wait(self.task, self.user, seconds=15)
        self.submit(result="post_not_available")
        self.assertEqual(TaskClaim.objects.get(task=self.task, user=self.user).status, TaskClaim.Status.POST_NOT_AVAILABLE)

    def test_repeated_open_does_not_restart_or_shift_the_recorded_time(self):
        from tasks.services import open_task_target
        first = open_task_target(task_id=self.task.pk, user=self.user)
        second = open_task_target(task_id=self.task.pk, user=self.user)
        self.assertEqual(first["opened_at"], second["opened_at"])

    def test_another_users_open_event_cannot_be_borrowed(self):
        claim_task(task_id=self.task.pk, user=self.other)
        open_and_wait(self.task, self.other)
        with self.assertRaises(ValidationError):
            self.submit()
        self.assertIsNone(TaskClaim.objects.get(task=self.task, user=self.user).target_opened_at)

    def test_open_event_cannot_be_used_for_a_different_task(self):
        from tasks.services import open_task_target
        other_task = Task.objects.create(target_link="https://example.com/other", reward_amount=Decimal("1"))
        open_and_wait(self.task, self.user)
        with self.assertRaises(ValidationError):
            self.submit(task=other_task)
        with self.assertRaises(ValidationError):
            open_task_target(task_id=other_task.pk, user=self.user)

    def test_invalid_target_link_never_records_an_open_event(self):
        from tasks.services import open_task_target
        Task.objects.filter(pk=self.task.pk).update(target_link="javascript:alert(1)")
        with self.assertRaises(ValidationError):
            open_task_target(task_id=self.task.pk, user=self.user)
        self.assertIsNone(TaskClaim.objects.get(task=self.task, user=self.user).target_opened_at)

    def test_api_ignores_client_supplied_timestamps(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(user=self.user)
        opened = client.post(f"/api/v1/tasks/{self.task.pk}/open-target/", {"opened_at": "2000-01-01T00:00:00Z"}, format="json")
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(opened.data["target_url"], "https://example.com/target")
        self.assertFalse(opened.data["submission_eligible"])
        early = client.post(f"/api/v1/tasks/{self.task.pk}/submit/", {"result": "work_done", "target_opened_at": "2000-01-01T00:00:00Z"}, format="json")
        self.assertEqual(early.status_code, 400)
        open_and_wait(self.task, self.user)
        mine = client.get("/api/v1/tasks/my/").data
        self.assertTrue(mine[0]["submission_eligible"])
        done = client.post(f"/api/v1/tasks/{self.task.pk}/submit/", {"result": "work_done"}, format="json")
        self.assertEqual(done.status_code, 200)
        self.assertEqual(done.data["status"], "work_submitted")
        again = client.post(f"/api/v1/tasks/{self.task.pk}/submit/", {"result": "work_done"}, format="json")
        self.assertEqual(again.status_code, 400)


class AvailableTasksVisibilityTests(TestCase):
    def setUp(self):
        from rest_framework.test import APIClient
        self.client = APIClient()
        self.first, self.second = make_user(1), make_user(2)
        self.task = Task.objects.create(target_link="https://example.com/shared", total_slots=2, remaining_slots=2, reward_amount=Decimal("1"))

    def available_ids(self, user):
        self.client.force_authenticate(user=user)
        return [item["id"] for item in self.client.get("/api/v1/tasks/available/").data["results"]]

    def test_claimed_task_leaves_claimers_list_but_stays_for_others_until_exhausted(self):
        claim_task(task_id=self.task.pk, user=self.first)
        self.assertNotIn(self.task.pk, self.available_ids(self.first))
        self.assertIn(self.task.pk, self.available_ids(self.second))
        claim_task(task_id=self.task.pk, user=self.second)
        third = make_user(3)
        self.assertNotIn(self.task.pk, self.available_ids(third))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.FULLY_CLAIMED)

    def test_available_list_is_capped_at_five(self):
        for index in range(7):
            Task.objects.create(target_link=f"https://example.com/paged-{index}")
        self.client.force_authenticate(user=self.first)
        response = self.client.get("/api/v1/tasks/available/?page_size=100")
        self.assertEqual(response.data["count"], 8)
        self.assertEqual(len(response.data["results"]), 5)


class TaskClaimConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        User = get_user_model()
        self.user_one = User.objects.create_user(
            email="race-one@example.com", password="TestPassword!9317", full_name="Race One",
            platform_username="race_one", whatsapp_number="9876543290", state="DL", gender="female",
            approval_status="approved", is_active=True,
        )
        self.user_two = User.objects.create_user(
            email="race-two@example.com", password="TestPassword!9317", full_name="Race Two",
            platform_username="race_two", whatsapp_number="9876543291", state="DL", gender="female",
            approval_status="approved", is_active=True,
        )
        self.task = Task.objects.create(target_link="https://example.com/race", total_slots=1, remaining_slots=1)

    def _race_claim(self, user_id, barrier):
        from django.db import close_old_connections
        close_old_connections()
        try:
            user = get_user_model().objects.get(pk=user_id)
            barrier.wait(timeout=5)
            claim_task(task_id=self.task.pk, user=user)
            return True
        except ValidationError:
            return False
        finally:
            close_old_connections()

    def test_two_users_racing_for_last_slot_produce_one_claim(self):
        from threading import Barrier
        from concurrent.futures import ThreadPoolExecutor
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda user_id: self._race_claim(user_id, barrier), [self.user_one.pk, self.user_two.pk]))
        self.task.refresh_from_db()
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(self.task.remaining_slots, 0)
        self.assertEqual(TaskClaim.objects.filter(task=self.task).count(), 1)

    def test_same_user_concurrent_claims_decrement_only_once(self):
        from threading import Barrier
        from concurrent.futures import ThreadPoolExecutor
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _index: self._race_claim(self.user_one.pk, barrier), range(2)))
        self.task.refresh_from_db()
        self.assertEqual(sum(outcomes), 1)
        self.assertEqual(self.task.remaining_slots, 0)
        self.assertEqual(TaskClaim.objects.filter(task=self.task, user=self.user_one).count(), 1)

    def test_three_users_racing_for_two_slots_produce_two_claims(self):
        from threading import Barrier
        from concurrent.futures import ThreadPoolExecutor
        Task.objects.filter(pk=self.task.pk).update(total_slots=2, remaining_slots=2)
        user_three = get_user_model().objects.create_user(
            email="race-three@example.com", password="TestPassword!9317", full_name="Race Three",
            platform_username="race_three", whatsapp_number="9876543292", state="DL", gender="female",
            approval_status="approved", is_active=True,
        )
        barrier = Barrier(3)
        with ThreadPoolExecutor(max_workers=3) as pool:
            outcomes = list(pool.map(lambda user_id: self._race_claim(user_id, barrier), [self.user_one.pk, self.user_two.pk, user_three.pk]))
        self.task.refresh_from_db()
        self.assertEqual(sum(outcomes), 2)
        self.assertEqual(self.task.remaining_slots, 0)
        self.assertEqual(self.task.status, Task.Status.FULLY_CLAIMED)
        self.assertEqual(TaskClaim.objects.filter(task=self.task).count(), 2)
