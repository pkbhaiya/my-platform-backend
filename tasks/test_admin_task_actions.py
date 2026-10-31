"""Admin Keep (resume) / Cancel actions, delivery figures and Target Not Available rates."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import close_old_connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from orders.models import Order
from tasks.models import Task, TaskClaim, TaskStrike
from tasks.services import approve_submitted_task, cancel_task, claim_task, submit_task_result
from tasks.test_claim_lifecycle import make_order_task, make_user, opened, statuses
from wallet.models import WalletTransaction

User = get_user_model()


def admin_client(admin):
    client = APIClient()
    client.force_authenticate(admin)
    return client


def submitted(task, user, result="work_done"):
    opened(task, user)
    submit_task_result(task_id=task.pk, user=user, result=result)
    return TaskClaim.objects.get(task=task, user=user)


class ResumeTests(TestCase):
    def setUp(self):
        self.admin = make_user(0, is_staff=True)
        self.api = admin_client(self.admin)
        self.order, self.task = make_order_task(2)

    def resume(self, task=None):
        return self.api.post(f"/api/v1/admin/tasks/{(task or self.task).pk}/resume/")

    def test_paused_task_becomes_available_and_resume_is_idempotent(self):
        Task.objects.filter(pk=self.task.pk).update(status=Task.Status.PAUSED)
        response = self.resume()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "available")
        self.assertEqual(self.resume().data["status"], "available")
        self.assertEqual(statuses(self.task, self.order), ("available", 2, "accepted", "available"))

    def test_paused_task_without_free_slots_becomes_fully_claimed(self):
        for index in (1, 2):
            claim_task(task_id=self.task.pk, user=make_user(index))
        Task.objects.filter(pk=self.task.pk).update(status=Task.Status.PAUSED)
        self.assertEqual(self.resume().data["status"], "fully_claimed")

    def test_only_paused_tasks_can_be_resumed(self):
        Task.objects.filter(pk=self.task.pk).update(status=Task.Status.COMPLETED)
        self.assertEqual(self.resume().status_code, 409)
        self.assertEqual(self.api.post("/api/v1/admin/tasks/999999/resume/").status_code, 404)
        worker = APIClient()
        worker.force_authenticate(make_user(5))
        self.assertEqual(worker.post(f"/api/v1/admin/tasks/{self.task.pk}/resume/").status_code, 403)


class CancelTests(TestCase):
    def setUp(self):
        self.admin = make_user(0, is_staff=True)
        self.api = admin_client(self.admin)
        self.order, self.task = make_order_task(4)
        self.approved_user, self.pending_user, self.active_user, self.tna_user = (make_user(i) for i in (1, 2, 3, 4))
        self.approved = submitted(self.task, self.approved_user)
        approve_submitted_task(task_id=self.approved.pk, reviewer=self.admin)
        self.pending = submitted(self.task, self.pending_user)
        self.tna = submitted(self.task, self.tna_user, "post_not_available")
        claim_task(task_id=self.task.pk, user=self.active_user)
        self.active = TaskClaim.objects.get(task=self.task, user=self.active_user)

    def cancel(self, **data):
        return self.api.post(f"/api/v1/admin/tasks/{self.task.pk}/cancel/", data, format="json")

    def test_reason_is_required_and_other_needs_a_note(self):
        self.assertEqual(self.cancel().status_code, 400)
        self.assertEqual(self.cancel(reason="nonsense").status_code, 400)
        response = self.cancel(reason="other", note="  ")
        self.assertEqual(response.status_code, 400)
        self.assertIn("note", response.data)
        self.assertEqual(statuses(self.task)[0], "available")

    def test_cancel_settles_claims_task_and_order_in_one_step(self):
        reward_rows = WalletTransaction.objects.count()
        response = self.cancel(reason="target_removed", note="Post deleted by owner")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "cancelled")
        self.assertEqual(response.data["order"]["fulfillment_status"], "partial")
        self.assertEqual((response.data["delivered"], response.data["remains"]), (1, 3))
        self.assertEqual(response.data["cancellation"]["reason_display"], "Target removed")
        self.assertEqual(response.data["cancellation"]["cancelled_by"], self.admin.email)

        for claim, expected in ((self.approved, "approved"), (self.pending, "cancelled"),
                                (self.active, "cancelled"), (self.tna, "post_not_available")):
            claim.refresh_from_db()
            self.assertEqual(claim.status, expected)
        self.assertEqual(WalletTransaction.objects.count(), reward_rows)
        self.assertFalse(TaskStrike.objects.exists())
        self.assertEqual(statuses(self.task, self.order), ("cancelled", 0, "partial", "unavailable"))
        self.task.refresh_from_db()
        self.assertEqual((self.task.cancel_reason, self.task.cancel_note, self.task.cancelled_by), ("target_removed", "Post deleted by owner", self.admin))
        self.assertIsNotNone(self.task.cancelled_at)

    def test_cancel_is_idempotent(self):
        first = self.cancel(reason="broken_link")
        second = self.cancel(reason="other", note="changed my mind")
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.data["cancellation"], first.data["cancellation"])

    def test_order_without_approved_work_is_cancelled_and_completed_tasks_cannot_be(self):
        order, task = make_order_task(1)
        claim_task(task_id=task.pk, user=make_user(9))
        response = self.api.post(f"/api/v1/admin/tasks/{task.pk}/cancel/", {"reason": "target_private"}, format="json")
        self.assertEqual(response.data["order"]["fulfillment_status"], "cancelled")
        completed_order, completed = make_order_task(1)
        approve_submitted_task(task_id=submitted(completed, make_user(10)).pk, reviewer=self.admin)
        self.assertEqual(statuses(completed)[0], "completed")
        self.assertEqual(self.api.post(f"/api/v1/admin/tasks/{completed.pk}/cancel/", {"reason": "broken_link"}, format="json").status_code, 409)

    def test_cancelled_claims_can_no_longer_act_and_free_their_owner(self):
        self.cancel(reason="target_removed")
        with self.assertRaises(ValidationError):
            submit_task_result(task_id=self.task.pk, user=self.active_user, result="work_done")
        with self.assertRaises(ValidationError):
            approve_submitted_task(task_id=self.pending.pk, reviewer=self.admin)
        other_order, other = make_order_task(1)
        claim_task(task_id=other.pk, user=self.active_user)
        self.assertEqual(statuses(self.task)[1], 0)

    def test_reports_show_delivery_cancellation_and_target_not_available_count(self):
        self.cancel(reason="broken_link")
        row = next(item for item in self.api.get("/api/v1/admin/tasks/history/").data["results"] if item["id"] == self.task.pk)
        self.assertEqual((row["delivered"], row["remains"], row["target_not_available_reports"]), (1, 3, 1))
        self.assertEqual(row["cancellation"]["reason"], "broken_link")
        summary = self.api.get(f"/api/v1/admin/tasks/{self.task.pk}/submissions/").data["task"]
        self.assertEqual((summary["delivered"], summary["target_not_available_reports"]), (1, 1))
        self.assertEqual(summary["cancellation"]["reason_display"], "Broken link")
        order_row = next(item for item in self.api.get("/api/v1/admin/orders/").data["results"] if item["id"] == self.order.pk)
        self.assertEqual((order_row["delivered"], order_row["remains"], order_row["fulfillment_status"]), (1, 3, "partial"))
        self.assertEqual(order_row["cancellation"]["reason"], "broken_link")


class PausedFirstTests(TestCase):
    def test_paused_tasks_lead_the_task_history(self):
        api = admin_client(make_user(0, is_staff=True))
        make_order_task(1)
        paused_order, paused = make_order_task(1)
        make_order_task(1)
        Task.objects.filter(pk=paused.pk).update(status=Task.Status.PAUSED)
        self.assertEqual(api.get("/api/v1/admin/tasks/history/").data["results"][0]["id"], paused.pk)
        self.assertEqual(api.get("/api/v1/admin/tasks/history/", {"status": "paused"}).data["count"], 1)


class TargetNotAvailableRateTests(TestCase):
    def test_rate_uses_the_last_20_submissions_and_flags_half_or_more(self):
        admin = make_user(0, is_staff=True)
        api = admin_client(admin)
        reporter, steady, newcomer = make_user(1), make_user(2), make_user(3)
        order, task = make_order_task(100)
        now = timezone.now()
        # 10 old Work Done submissions fall outside reporter's last 20.
        for minutes, status in [(300 + i, "approved") for i in range(10)] + [(100 + i, "post_not_available") for i in range(10)] + [(i, "work_submitted") for i in range(10)]:
            TaskClaim.objects.create(task=task, user=reporter, status=status, submitted_at=now - timedelta(minutes=minutes))
        for minutes in range(5):
            TaskClaim.objects.create(task=task, user=steady, status="approved", submitted_at=now - timedelta(minutes=minutes))
        TaskClaim.objects.create(task=task, user=steady, status="post_not_available", submitted_at=now)
        # Shown in the user detail view (Phase 11: lean list).
        rows = {user.pk: api.get(f"/api/v1/admin/users/{user.pk}/").data["target_not_available_rate"] for user in (reporter, steady, newcomer)}
        self.assertEqual(rows[reporter.pk], {"recent_submissions": 20, "target_not_available": 10, "rate_percent": "50.0", "flagged": True})
        self.assertEqual(rows[steady.pk], {"recent_submissions": 6, "target_not_available": 1, "rate_percent": "16.7", "flagged": False})
        self.assertEqual(rows[newcomer.pk], {"recent_submissions": 0, "target_not_available": 0, "rate_percent": None, "flagged": False})


class ConcurrentCancelTests(TransactionTestCase):
    def setUp(self):
        self.admin = make_user(0, is_staff=True)
        self.order, self.task = make_order_task(2)
        self.claim = submitted(self.task, make_user(1))

    def run_together(self, functions):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        barrier = Barrier(len(functions))

        def run(function):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                return function()
            except ValidationError as exc:
                return exc
            finally:
                close_old_connections()

        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run, functions))

    def test_cancel_racing_an_approval_never_pays_a_cancelled_claim(self):
        self.run_together([
            lambda: approve_submitted_task(task_id=self.claim.pk, reviewer=self.admin),
            lambda: cancel_task(task_id=self.task.pk, reviewer=self.admin, reason="broken_link"),
        ])
        self.claim.refresh_from_db()
        credited = WalletTransaction.objects.filter(claim=self.claim).count()
        self.assertEqual(credited, 1 if self.claim.status == "approved" else 0)
        self.assertIn(self.claim.status, ("approved", "cancelled"))
        self.assertEqual(statuses(self.task)[0], "cancelled")
        self.order.refresh_from_db()
        self.assertEqual(self.order.fulfillment_status, "partial" if credited else "cancelled")

    def test_concurrent_cancels_cancel_once(self):
        results = self.run_together([
            lambda: cancel_task(task_id=self.task.pk, reviewer=self.admin, reason=reason)
            for reason in ("broken_link", "target_removed", "target_private")
        ])
        self.assertTrue(all(isinstance(result, Task) for result in results), results)
        self.task.refresh_from_db()
        self.assertEqual(len({result.cancel_reason for result in results}), 1)
        self.assertEqual(self.task.cancel_reason, results[0].cancel_reason)
        self.assertEqual(Order.objects.get(pk=self.order.pk).fulfillment_status, "cancelled")
