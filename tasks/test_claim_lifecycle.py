"""Claim lifecycle: slot release, claim timeout, Target Not Available, auto-pause and completion."""

import importlib
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from common.models import SiteConfiguration
from orders.models import Order
from services_catalog.models import Service
from tasks.models import Task, TaskClaim, TaskStrike, TaskUserClaim
from tasks.services import (
    CLAIM_EXPIRED_MESSAGE,
    approve_submitted_task,
    claim_task,
    expire_stale_claims,
    reject_submitted_task,
    release_slot,
    submit_task_result,
)
from wallet.models import WalletTransaction

User = get_user_model()


def make_user(index, **extra):
    fields = dict(
        email=f"life-{index}@example.com", password="TestPassword!9317", full_name=f"Life {index}",
        platform_username=f"life_{index}", whatsapp_number=f"91234{index:05d}", state="DL",
        gender="female", approval_status="approved", is_active=True,
    )
    fields.update(extra)
    return User.objects.create_user(**fields)


def make_order_task(slots):
    service = Service.objects.create(name="Lifecycle", price_per_1000=Decimal("100.0000"), minimum_order_quantity=1)
    order = Order.objects.create(
        service=service, source=Order.Source.MANUAL, target_link="https://example.com/life", quantity=slots,
        price_per_1000_snapshot=Decimal("100.0000"), business_share_percentage_snapshot=Decimal("75.00"),
        user_reward_percentage_snapshot=Decimal("25.00"), capacity_percentage_snapshot=Decimal("100.00"),
        total_order_value=Decimal("100.0000") * slots / 1000,
    )
    task = Task.objects.create(order=order, target_link=order.target_link, total_slots=slots, remaining_slots=slots,
                               reward_amount=Decimal("0.02500000"))
    return order, task


def opened(task, user):
    """Claim and move the Open Target event far enough into the past to submit."""
    claim_task(task_id=task.pk, user=user)
    TaskClaim.objects.filter(task=task, user=user, status=TaskClaim.Status.CLAIMED).update(
        target_opened_at=timezone.now() - timedelta(seconds=60), target_wait_seconds=15)


def age_claims(task, minutes):
    TaskClaim.objects.filter(task=task, status=TaskClaim.Status.CLAIMED).update(
        claimed_at=timezone.now() - timedelta(minutes=minutes))


def statuses(task, order=None):
    task.refresh_from_db()
    result = (task.status, task.remaining_slots)
    if order is not None:
        order.refresh_from_db()
        result += (order.fulfillment_status, order.availability_status)
    return result


class SlotReleaseTests(TestCase):
    def setUp(self):
        self.admin = make_user(0, is_staff=True)
        self.order, self.task = make_order_task(2)
        self.workers = [make_user(index) for index in (1, 2, 3)]

    def test_rejection_releases_the_slot_issues_a_strike_and_reopens_a_fully_claimed_task(self):
        for worker in self.workers[:2]:
            opened(self.task, worker)
            submit_task_result(task_id=self.task.pk, user=worker, result="work_done")
        self.assertEqual(statuses(self.task, self.order), ("fully_claimed", 0, "in_progress", "fully_claimed"))
        rejected = TaskClaim.objects.get(task=self.task, user=self.workers[0])
        reject_submitted_task(task_id=rejected.pk, reviewer=self.admin)
        self.assertEqual(statuses(self.task, self.order), ("available", 1, "in_progress", "partially_claimed"))
        self.assertEqual(TaskStrike.objects.filter(claim=rejected).count(), 1)
        # The freed slot is claimable by someone new.
        claim_task(task_id=self.task.pk, user=self.workers[2])
        self.assertEqual(statuses(self.task)[1], 0)

    def test_target_not_available_releases_the_slot_without_strike_reward_or_review(self):
        opened(self.task, self.workers[0])
        submit_task_result(task_id=self.task.pk, user=self.workers[0], result="post_not_available")
        claim = TaskClaim.objects.get(task=self.task, user=self.workers[0])
        self.assertEqual(claim.status, TaskClaim.Status.POST_NOT_AVAILABLE)
        self.assertEqual(claim.get_status_display(), "Target Not Available")
        self.assertEqual(statuses(self.task, self.order), ("available", 2, "in_progress", "available"))
        self.assertFalse(TaskStrike.objects.exists())
        self.assertFalse(WalletTransaction.objects.exists())
        admin = APIClient()
        admin.force_authenticate(self.admin)
        self.assertEqual(admin.get("/api/v1/admin/tasks/pending/").data["count"], 0)
        with self.assertRaises(ValidationError):
            approve_submitted_task(task_id=claim.pk, reviewer=self.admin)

    def test_a_release_can_never_exceed_total_slots(self):
        self.assertEqual(release_slot(self.task.pk, timezone.now()), 0)
        self.assertEqual(statuses(self.task)[1], 2)

    def test_a_user_can_never_reclaim_after_rejection_target_not_available_or_expiry(self):
        reject_user, tna_user, expired_user = self.workers
        opened(self.task, reject_user)
        submit_task_result(task_id=self.task.pk, user=reject_user, result="work_done")
        reject_submitted_task(task_id=TaskClaim.objects.get(user=reject_user).pk, reviewer=self.admin)
        opened(self.task, tna_user)
        submit_task_result(task_id=self.task.pk, user=tna_user, result="post_not_available")
        claim_task(task_id=self.task.pk, user=expired_user)
        age_claims(self.task, 31)
        self.assertEqual(expire_stale_claims(), 1)
        for user in self.workers:
            with self.assertRaises(ValidationError):
                claim_task(task_id=self.task.pk, user=user)
        self.assertEqual(statuses(self.task)[1], 2)


class CompletionTests(TestCase):
    def setUp(self):
        self.admin = make_user(0, is_staff=True)
        self.order, self.task = make_order_task(2)
        self.workers = [make_user(index) for index in (1, 2)]
        for worker in self.workers:
            opened(self.task, worker)
            submit_task_result(task_id=self.task.pk, user=worker, result="work_done")

    def test_completed_only_when_every_slot_is_approved(self):
        first, second = TaskClaim.objects.filter(task=self.task).order_by("id")
        approve_submitted_task(task_id=first.pk, reviewer=self.admin)
        self.assertEqual(statuses(self.task, self.order), ("fully_claimed", 0, "in_progress", "fully_claimed"))
        approve_submitted_task(task_id=second.pk, reviewer=self.admin)
        self.assertEqual(statuses(self.task, self.order), ("completed", 0, "completed", "fully_claimed"))


class ClaimTimeoutTests(TestCase):
    def setUp(self):
        self.order, self.task = make_order_task(1)
        self.other_order, self.other_task = make_order_task(1)
        self.worker = make_user(1)
        self.api = APIClient()
        self.api.force_authenticate(self.worker)

    def test_a_stale_claim_expires_and_releases_its_slot(self):
        claim_task(task_id=self.task.pk, user=self.worker)
        age_claims(self.task, 29)
        self.assertEqual(expire_stale_claims(), 0)
        age_claims(self.task, 31)
        self.assertEqual(expire_stale_claims(), 1)
        claim = TaskClaim.objects.get(task=self.task)
        self.assertEqual(claim.status, TaskClaim.Status.EXPIRED)
        self.assertEqual(statuses(self.task, self.order), ("available", 1, "in_progress", "available"))
        self.assertEqual(expire_stale_claims(), 0)

    def test_the_configured_timeout_is_used(self):
        config = SiteConfiguration.load()
        config.claim_timeout_minutes = 5
        config.save()
        claim_task(task_id=self.task.pk, user=self.worker)
        age_claims(self.task, 6)
        self.assertEqual(expire_stale_claims(), 1)

    def test_available_tasks_my_tasks_and_claim_requests_expire_lazily(self):
        for path in ("/api/v1/tasks/available/", "/api/v1/tasks/my/"):
            TaskClaim.objects.all().delete()
            Task.objects.filter(pk=self.task.pk).update(remaining_slots=1, status="available")
            TaskUserClaim.objects.all().delete()
            claim_task(task_id=self.task.pk, user=self.worker)
            age_claims(self.task, 31)
            self.assertEqual(self.api.get(path).status_code, 200)
            self.assertEqual(TaskClaim.objects.get(task=self.task).status, TaskClaim.Status.EXPIRED, path)
        # Claiming another Task frees the user's stale active claim first.
        self.assertEqual(self.api.post(f"/api/v1/tasks/{self.other_task.pk}/claim/").status_code, 200)

    def test_submit_and_open_target_reject_an_expired_claim(self):
        opened(self.task, self.worker)
        age_claims(self.task, 31)
        for path, body in ((f"/api/v1/tasks/{self.task.pk}/submit/", {"result": "work_done"}),
                           (f"/api/v1/tasks/{self.task.pk}/open-target/", {})):
            response = self.api.post(path, body, format="json")
            self.assertEqual(response.status_code, 400, path)
            self.assertEqual(response.data["detail"], [CLAIM_EXPIRED_MESSAGE])
        self.assertEqual(TaskClaim.objects.get(task=self.task).status, TaskClaim.Status.EXPIRED)
        self.assertEqual(statuses(self.task)[1], 1)

    def test_my_tasks_reports_the_claim_time_limit(self):
        claim_task(task_id=self.task.pk, user=self.worker)
        row = self.api.get("/api/v1/tasks/my/").data[0]
        self.assertEqual(row["claim_timeout_minutes"], 30)
        self.assertIsNotNone(row["expires_at"])
        self.assertTrue(29 * 60 < row["expires_in_seconds"] <= 30 * 60)
        history_row = TaskClaim.objects.get(task=self.task)
        self.assertEqual(history_row.status, TaskClaim.Status.CLAIMED)

    def test_expire_claims_command(self):
        claim_task(task_id=self.task.pk, user=self.worker)
        age_claims(self.task, 31)
        out = StringIO()
        call_command("expire_claims", stdout=out)
        self.assertIn("Expired 1 claim(s) and 0 order(s).", out.getvalue())


class AutoPauseTests(TestCase):
    def setUp(self):
        self.admin = make_user(0, is_staff=True)
        self.order, self.task = make_order_task(20)
        self.workers = [make_user(index) for index in range(1, 25)]
        self.next_worker = iter(self.workers)

    def submit(self, result):
        worker = next(self.next_worker)
        opened(self.task, worker)
        submit_task_result(task_id=self.task.pk, user=worker, result=result)
        return worker

    def test_pauses_once_reports_reach_the_minimum_and_the_percentage(self):
        self.submit("post_not_available")
        self.submit("post_not_available")
        self.assertEqual(statuses(self.task)[0], "available")
        self.submit("post_not_available")
        self.assertEqual(statuses(self.task, self.order)[0::2], ("paused", "in_progress"))
        self.order.refresh_from_db()
        self.assertEqual(self.order.availability_status, Order.AvailabilityStatus.UNAVAILABLE)

    def test_does_not_pause_below_the_percentage(self):
        for _ in range(13):
            self.submit("work_done")
        for _ in range(3):
            self.submit("post_not_available")
        # 3 reports of 16 submissions = 18.75% < 20%.
        self.assertEqual(statuses(self.task)[0], "available")
        self.submit("post_not_available")
        # 4 of 17 = 23.5%.
        self.assertEqual(statuses(self.task)[0], "paused")

    def test_paused_tasks_are_hidden_and_unclaimable_but_existing_claims_can_submit(self):
        holder = next(self.next_worker)
        opened(self.task, holder)
        for _ in range(3):
            self.submit("post_not_available")
        self.assertEqual(statuses(self.task)[0], "paused")
        newcomer = next(self.next_worker)
        api = APIClient()
        api.force_authenticate(newcomer)
        self.assertEqual(api.get("/api/v1/tasks/available/").data["count"], 0)
        self.assertEqual(api.post(f"/api/v1/tasks/{self.task.pk}/claim/").status_code, 400)
        submit_task_result(task_id=self.task.pk, user=holder, result="work_done")
        self.assertEqual(TaskClaim.objects.get(user=holder).status, TaskClaim.Status.WORK_SUBMITTED)
        self.assertEqual(statuses(self.task)[0], "paused")

    def test_custom_thresholds_from_site_configuration(self):
        config = SiteConfiguration.load()
        config.tna_pause_min_reports = 1
        config.tna_pause_percent = 100
        config.save()
        self.submit("work_done")
        self.submit("post_not_available")
        self.assertEqual(statuses(self.task)[0], "available")
        order, task = make_order_task(5)
        worker = next(self.next_worker)
        opened(task, worker)
        submit_task_result(task_id=task.pk, user=worker, result="post_not_available")
        self.assertEqual(statuses(task)[0], "paused")


class SettingsValidationTests(TestCase):
    def setUp(self):
        self.api = APIClient()
        self.api.force_authenticate(make_user(0, is_staff=True))

    def patch(self, **data):
        return self.api.patch("/api/v1/admin/platform-settings/", data, format="json")

    def test_ranges(self):
        for field, bad, good in (("claim_timeout_minutes", 4, 5), ("claim_timeout_minutes", 1441, 1440),
                                 ("tna_pause_min_reports", 0, 1), ("tna_pause_percent", 101, 100)):
            self.assertEqual(self.patch(**{field: bad}).status_code, 400, (field, bad))
            self.assertEqual(self.patch(**{field: good}).status_code, 200, (field, good))

    def test_claim_timeout_must_outlive_the_target_wait(self):
        response = self.patch(target_wait_seconds=600, claim_timeout_minutes=10)
        self.assertEqual(response.status_code, 400)
        self.assertIn("claim_timeout_minutes", response.data)
        self.assertEqual(self.patch(target_wait_seconds=599, claim_timeout_minutes=10).status_code, 200)


class LegacyOrderMigrationTests(TestCase):
    def test_recomputes_only_completed_and_available_orders(self):
        migration = importlib.import_module("tasks.migrations.0012_recompute_legacy_completed_orders")
        approved_order, approved_task = make_order_task(1)
        rejected_order, rejected_task = make_order_task(2)
        untouched_order, untouched_task = make_order_task(1)
        for order in (approved_order, rejected_order, untouched_order):
            Order.objects.filter(pk=order.pk).update(fulfillment_status="completed", availability_status="available")
        Order.objects.filter(pk=untouched_order.pk).update(availability_status="fully_claimed")
        Task.objects.all().update(status="completed", remaining_slots=0)
        TaskClaim.objects.create(task=approved_task, user=make_user(1), status="approved")
        TaskClaim.objects.create(task=rejected_task, user=make_user(2), status="rejected")
        TaskClaim.objects.create(task=rejected_task, user=make_user(3), status="post_not_available")
        TaskClaim.objects.create(task=untouched_task, user=make_user(4), status="rejected")

        migration.recompute_legacy_completed_orders(django_apps, None)

        self.assertEqual(statuses(approved_task, approved_order), ("completed", 0, "completed", "fully_claimed"))
        self.assertEqual(statuses(rejected_task, rejected_order), ("available", 2, "in_progress", "available"))
        self.assertEqual(statuses(untouched_task, untouched_order), ("completed", 0, "completed", "fully_claimed"))


class ConcurrentLifecycleTests(TransactionTestCase):
    def setUp(self):
        self.admin = make_user(0, is_staff=True)
        self.order, self.task = make_order_task(2)

    def run_together(self, functions):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(len(functions))

        def run(function):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                return function()
            except ValidationError:
                return None
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run, functions))

    def test_concurrent_expiry_sweeps_release_each_slot_once(self):
        for index in (1, 2):
            claim_task(task_id=self.task.pk, user=make_user(index))
        age_claims(self.task, 31)
        results = self.run_together([expire_stale_claims] * 3)
        self.assertEqual(sum(results), 2)
        self.assertEqual(TaskClaim.objects.filter(status=TaskClaim.Status.EXPIRED).count(), 2)
        self.assertEqual(statuses(self.task)[1], 2)

    def test_concurrent_reviews_of_one_claim_release_its_slot_once(self):
        worker = make_user(1)
        opened(self.task, worker)
        submit_task_result(task_id=self.task.pk, user=worker, result="work_done")
        claim_id = TaskClaim.objects.get(user=worker).pk
        self.run_together([lambda: reject_submitted_task(task_id=claim_id, reviewer=self.admin)] * 3)
        self.assertEqual(TaskStrike.objects.count(), 1)
        self.assertEqual(statuses(self.task)[1], 2)


class DeadlineWithoutSweepTests(TestCase):
    def test_submit_after_the_deadline_is_refused_even_if_no_sweep_ran(self):
        from unittest.mock import patch
        order, task = make_order_task(1)
        worker = make_user(1)
        opened(task, worker)
        age_claims(task, 31)
        with patch("tasks.services.expire_stale_claims", return_value=0):
            with self.assertRaisesMessage(ValidationError, CLAIM_EXPIRED_MESSAGE):
                submit_task_result(task_id=task.pk, user=worker, result="work_done")
        self.assertEqual(TaskClaim.objects.get(user=worker).status, TaskClaim.Status.CLAIMED)
