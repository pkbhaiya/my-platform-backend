"""Regression tests for the security and correctness fixes from the October 2026 code review."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from accounts.models import UserLevel
from orders.testing import make_admin_wallet
from orders.models import Order
from orders.services import get_eligible_user_count
from services_catalog.models import Service

User = get_user_model()


def make_user(email, **extra):
    fields = dict(
        email=email, password="TestPassword!9317", full_name="Order User",
        platform_username=email.split("@")[0], whatsapp_number="9123400001",
        state="DL", gender="female", approval_status="approved", is_active=True,
    )
    fields.update(extra)
    return User.objects.create_user(**fields)


def make_service(**extra):
    fields = dict(
        name="Review Service", price_per_1000=Decimal("100.0000"), minimum_order_quantity=1,
        capacity_percentage=Decimal("100.00"), business_share_percentage=Decimal("75.00"),
        user_reward_percentage=Decimal("25.00"), order_source=Service.OrderSource.BOTH,
    )
    fields.update(extra)
    return Service.objects.create(**fields)


class IncomingAPIKeyTests(APITestCase):
    def setUp(self):
        self.reseller, self.api_key = make_admin_wallet()

    def test_non_ascii_api_key_is_rejected_without_a_server_error(self):
        response = self.client.get("/api/v1/services/incoming/", HTTP_X_API_KEY="café-key")
        self.assertEqual(response.status_code, 403)

    def test_correct_api_key_is_still_accepted(self):
        response = self.client.get("/api/v1/services/incoming/", HTTP_X_API_KEY=self.api_key)
        self.assertEqual(response.status_code, 200)

    def test_service_with_inactive_level_lists_zero_capacity_instead_of_failing(self):
        make_user("worker@example.com")
        UserLevel.objects.create(code="gold", name="Gold", minimum_approved_tasks=10, display_order=1)
        healthy = make_service(name="Open to all")
        stranded = make_service(name="Gold only", eligible_level="gold")
        # Deactivated directly (as the Django admin's editable list allows),
        # bypassing the API guard against disabling a level in use.
        UserLevel.objects.filter(code="gold").update(is_active=False)
        response = self.client.get("/api/v1/services/incoming/", HTTP_X_API_KEY=self.api_key)
        self.assertEqual(response.status_code, 200)
        maximums = {row["service_id"]: row["maximum_quantity"] for row in response.data}
        self.assertEqual(maximums, {healthy.pk: 1, stranded.pk: 0})


class CapacityTests(APITestCase):
    def test_administrators_do_not_count_as_eligible_workers(self):
        make_user("worker-1@example.com")
        make_user("staff@example.com", is_staff=True)
        make_user("root@example.com", is_staff=True, is_superuser=True)
        self.assertEqual(get_eligible_user_count(make_service()), 1)


class TargetLinkTests(APITestCase):
    def test_non_http_target_links_are_refused(self):
        make_user("worker-2@example.com")
        admin = make_user("order-admin@example.com", is_staff=True)
        self.client.force_authenticate(admin)
        service = make_service()
        response = self.client.post("/api/v1/admin/orders/create/", {
            "service_id": service.pk, "target_link": "ftp://example.com/post", "quantity": 1,
            "idempotency_key": "2f1b8c4e-8a52-4c1b-9a8e-6f4b1f9d2c11",
        }, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], ["Enter a valid target URL."])
        self.assertFalse(Order.objects.exists())
