"""Available Tasks: oldest order first, across the 5-per-page pages."""

from datetime import timedelta
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from orders.models import Order
from tasks.models import Task
from tasks.test_claim_lifecycle import make_order_task, make_user

AVAILABLE = "/api/v1/tasks/available/"


class AvailableTasksOrderTests(APITestCase):
    def setUp(self):
        self.client.force_authenticate(make_user(1))
        self.base = timezone.now() - timedelta(days=10)

    def order_task(self, hours_after_base):
        order, task = make_order_task(1)
        Order.objects.filter(pk=order.pk).update(created_at=self.base + timedelta(hours=hours_after_base))
        return task

    def standalone_task(self, hours_after_base):
        task = Task.objects.create(title="Direct", target_link="https://example.com/direct", total_slots=1,
                                   remaining_slots=1, reward_amount=Decimal("0.02500000"))
        Task.objects.filter(pk=task.pk).update(created_at=self.base + timedelta(hours=hours_after_base))
        return task

    def listed_ids(self):
        ids, url = [], AVAILABLE
        while url:
            page = self.client.get(url).data
            ids.append([row["id"] for row in page["results"]])
            url = page["next"]
        return ids

    def test_oldest_order_first_across_pages_then_tasks_without_an_order(self):
        # Created newest-order-first, so ids run opposite to order age.
        by_age = [self.order_task(hours) for hours in (7, 6, 5, 4, 3, 2, 1)][::-1]
        direct_newer = self.standalone_task(0)
        direct_older = self.standalone_task(-5)  # older than every order, still listed after them
        self.assertEqual(self.listed_ids(), [
            [task.pk for task in by_age[:5]],
            [by_age[5].pk, by_age[6].pk, direct_older.pk, direct_newer.pk],
        ])

    def test_orders_created_at_the_same_moment_are_listed_by_task_id(self):
        first, second = self.order_task(1), self.order_task(1)
        self.assertEqual(self.listed_ids(), [[first.pk, second.pk]])
