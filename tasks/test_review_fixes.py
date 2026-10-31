"""Regression tests for fixes from the October 2026 code review."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APITestCase

from orders.models import Order
from services_catalog.models import Service
from tasks.models import Task, TaskClaim
from tasks.services import claim_task

User = get_user_model()


def make_user(email, **extra):
    fields = dict(
        email=email, password="TestPassword!9317", full_name="Task User",
        platform_username=email.split("@")[0], whatsapp_number="9123400003",
        state="DL", gender="female", approval_status="approved", is_active=True,
    )
    fields.update(extra)
    return User.objects.create_user(**fields)


def make_order_task(service, index):
    order = Order.objects.create(
        service=service, source=Order.Source.MANUAL, target_link=f"https://example.com/{index}", quantity=1,
        price_per_1000_snapshot=Decimal("100.0000"), business_share_percentage_snapshot=Decimal("75.00"),
        user_reward_percentage_snapshot=Decimal("25.00"), capacity_percentage_snapshot=Decimal("100.00"),
    )
    return Task.objects.create(order=order, target_link=order.target_link, reward_amount=Decimal("0.025"))


class ClaimRecheckTests(APITestCase):
    def test_claim_uses_the_current_account_status_not_the_loaded_one(self):
        user = make_user("stale@example.com")
        task = Task.objects.create(title="Open", target_link="https://example.com/t", reward_amount=Decimal("1"))
        # Disapproved by a concurrent strike after this request loaded the user.
        User.objects.filter(pk=user.pk).update(approval_status=User.ApprovalStatus.DISAPPROVED)
        with self.assertRaises(ValidationError):
            claim_task(task_id=task.pk, user=user)
        task.refresh_from_db()
        self.assertEqual(task.remaining_slots, 1)
        self.assertFalse(TaskClaim.objects.exists())


class AdminTaskViewTests(APITestCase):
    def setUp(self):
        self.admin = make_user("task-admin@example.com", is_staff=True)
        self.client.force_authenticate(self.admin)

    def test_review_of_a_missing_submission_is_404(self):
        response = self.client.post("/api/v1/admin/task-submissions/999999/review/", {"action": "approve"}, format="json")
        self.assertEqual(response.status_code, 404)

    def review_history_queries(self, rows):
        worker = make_user(f"history-{rows}@example.com")
        for index in range(rows):
            service = Service.objects.create(name=f"Service {rows}-{index}", price_per_1000=Decimal("100.0000"))
            task = make_order_task(service, f"{rows}-{index}")
            TaskClaim.objects.create(
                task=task, user=worker, status=TaskClaim.Status.APPROVED,
                reviewed_at=timezone.now(), reviewed_by=self.admin,
            )
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/v1/admin/tasks/review-history/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"Service {rows}-0", {row["task_name"] for row in response.data["results"]})
        TaskClaim.objects.all().delete()
        return len(queries)

    def test_review_history_query_count_does_not_grow_with_rows(self):
        self.assertEqual(self.review_history_queries(1), self.review_history_queries(4))
