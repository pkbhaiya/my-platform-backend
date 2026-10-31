import json
from datetime import timedelta
from decimal import Decimal

from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from services_catalog.models import Service
from tasks.models import Task, TaskClaim
from tasks.services import create_tasks_for_order
from tasks.test_worker_dashboard import make_order, make_service, make_user

URL = "/api/v1/public/overview/"


class PublicOverviewTests(APITestCase):
    """The landing page's only data source: anonymous, aggregate, read-only."""

    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.workers = [make_user(index) for index in range(1, 4)]
        make_user(10, approval_status="pending")
        make_user(11, approval_status="approved", is_active=False)
        make_user(12, is_staff=True)
        self.service = make_service(name="Instagram Likes", description="Like the linked post.")
        manual = make_service(name="Manual Only Service")
        Service.objects.filter(pk=manual.pk).update(order_source="manual")
        inactive = make_service(name="Retired Service")
        Service.objects.filter(pk=inactive.pk).update(is_active=False)
        self.order = make_order(self.service, quantity=3)
        create_tasks_for_order(self.order.pk)
        self.task = Task.objects.get(order=self.order)
        Task.objects.filter(pk=self.task.pk).update(title="Private client brief")

    def approve(self, user, reviewed_at):
        return TaskClaim.objects.create(task=self.task, user=user, status=TaskClaim.Status.APPROVED, reviewed_at=reviewed_at)

    def test_anonymous_visitor_gets_real_aggregates(self):
        now = timezone.now()
        self.approve(self.workers[0], now - timedelta(hours=1))
        self.approve(self.workers[1], now - timedelta(days=30))
        TaskClaim.objects.create(task=self.task, user=self.workers[2], status=TaskClaim.Status.REJECTED, reviewed_at=now)

        response = APIClient().get(URL)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["stats"], {
            "open_tasks": 1,
            "open_slots": 3,
            "approved_submissions": 2,
            "approved_workers": 3,
            "services": 1,
        })
        self.assertEqual([service["name"] for service in response.data["services"]], ["Instagram Likes"])
        self.assertEqual(response.data["services"][0], {
            "name": "Instagram Likes",
            "description": "Like the linked post.",
            "rate_per_1000": "400.0000",
            "currency": "INR",
            "minimum_quantity": 1,
        })
        self.assertTrue(response.data["service_rates_public"])

    def test_no_identities_links_titles_or_amounts_are_exposed(self):
        self.approve(self.workers[0], timezone.now() - timedelta(minutes=10))

        body = json.dumps(APIClient().get(URL).data)

        for private in ("worker-1@example.com", "worker_1", "Worker 1", "https://example.com/post", "Private client brief", "reward", "email", "target_link", "order"):
            self.assertNotIn(private, body)

    def test_recent_activity_lists_only_recent_approvals_at_minute_precision(self):
        now = timezone.now()
        for minutes in range(1, 8):
            self.approve(self.workers[minutes % 3], now - timedelta(minutes=minutes, seconds=17))
        self.approve(self.workers[0], now - timedelta(days=8))
        TaskClaim.objects.create(task=self.task, user=self.workers[1], status=TaskClaim.Status.REJECTED, reviewed_at=now)

        activity = APIClient().get(URL).data["recent_activity"]

        self.assertEqual(len(activity), 5)
        self.assertTrue(all(set(event) == {"type", "at"} for event in activity))
        self.assertTrue(all(event["type"] == "submission_approved" for event in activity))
        self.assertTrue(all(":00+" in event["at"] or event["at"].endswith(":00Z") for event in activity))
        self.assertEqual(activity, sorted(activity, key=lambda event: event["at"], reverse=True))

    @override_settings(PUBLIC_SERVICE_RATES=False)
    def test_rates_are_withheld_when_disabled(self):
        response = APIClient().get(URL)

        self.assertFalse(response.data["service_rates_public"])
        self.assertEqual(response.data["services"][0]["name"], "Instagram Likes")
        self.assertIsNone(response.data["services"][0]["rate_per_1000"])
        self.assertNotIn("400", json.dumps(response.data["services"]))

    def test_stale_or_invalid_tokens_do_not_block_the_public_page(self):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Bearer not-a-valid-token")

        self.assertEqual(client.get(URL).status_code, 200)

    def test_endpoint_is_read_only(self):
        client = APIClient()
        for method in (client.post, client.put, client.patch, client.delete):
            self.assertEqual(method(URL, {}, format="json").status_code, 405)

    def test_rates_are_serialised_exactly(self):
        Service.objects.filter(pk=self.service.pk).update(price_per_1000=Decimal("12345.0125"))

        self.assertEqual(APIClient().get(URL).data["services"][0]["rate_per_1000"], "12345.0125")
