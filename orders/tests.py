from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from .models import Order
from .testing import make_admin_wallet
from services_catalog.models import Service
from tasks.models import Task


class IncomingOrderAPITests(APITestCase):
    def setUp(self):
        self.reseller, self.api_key = make_admin_wallet()
        User = get_user_model()
        User.objects.create_user(
            email="eligible@example.com",
            password="TestPassword!9317",
            full_name="Eligible User",
            platform_username="eligible_user",
            whatsapp_number="9876543201",
            state="DL",
            gender="female",
            approval_status="approved",
            is_active=True,
        )
        self.service = Service.objects.create(
            name="API Test Service",
            price_per_1000=Decimal("12.3456"),
            minimum_order_quantity=1,
            capacity_percentage=Decimal("100.00"),
            business_share_percentage=Decimal("75.00"),
            user_reward_percentage=Decimal("25.00"),
            order_source=Service.OrderSource.API,
        )
        self.payload = {
            "service_id": self.service.pk,
            "external_order_id": "external-001",
            "target_link": "https://example.com/post",
            "quantity": 1,
        }

    def test_missing_api_key_is_rejected(self):
        response = self.client.post(
            "/api/v1/orders/incoming/", self.payload, format="json"
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Order.objects.count(), 0)

    def test_incoming_services_returns_only_active_api_services(self):
        self.service.is_active = False
        self.service.save()
        active = Service.objects.create(
            name="Active API Service", price_per_1000=Decimal("20.0000"),
            minimum_order_quantity=2, capacity_percentage=Decimal("100.00"),
            business_share_percentage=Decimal("75.00"), user_reward_percentage=Decimal("25.00"),
            order_source=Service.OrderSource.API, target_link_required=False,
        )
        Service.objects.create(
            name="Manual Only", price_per_1000=Decimal("20.0000"),
            minimum_order_quantity=1, capacity_percentage=Decimal("100.00"),
            business_share_percentage=Decimal("75.00"), user_reward_percentage=Decimal("25.00"),
            order_source=Service.OrderSource.MANUAL,
        )

        response = self.client.get("/api/v1/services/incoming/", HTTP_X_API_KEY=self.api_key)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["service_id"] for row in response.data], [active.pk])
        self.assertEqual(response.data[0]["rate_unit"], "INR per 1,000 units")
        self.assertFalse(response.data[0]["target_link_required"])

    def test_disabled_service_rejects_new_external_orders(self):
        self.service.is_active = False
        self.service.save()
        response = self.client.post(
            "/api/v1/orders/incoming/", self.payload, format="json",
            HTTP_X_API_KEY=self.api_key,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Task.objects.count(), 0)

    def test_order_pricing_is_server_calculated_and_retry_is_idempotent(self):
        response = self.client.post(
            "/api/v1/orders/incoming/",
            self.payload,
            format="json",
            HTTP_X_API_KEY=self.api_key,
        )

        self.assertEqual(response.status_code, 200)
        order = Order.objects.get(external_order_id="external-001")
        self.assertEqual(order.total_order_value, Decimal("0.01234560"))
        self.assertEqual(Task.objects.filter(order=order).count(), 1)
        self.assertEqual(Task.objects.get(order=order).total_slots, 1)

        retry = self.client.post(
            "/api/v1/orders/incoming/",
            self.payload,
            format="json",
            HTTP_X_API_KEY=self.api_key,
        )

        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data["order"], order.pk)
        self.assertEqual(Task.objects.filter(order=order).count(), 1)

    def test_quantity_100_creates_one_task_with_100_slots_and_retry_does_not_duplicate(self):
        User = get_user_model()
        for index in range(99):
            User.objects.create_user(
                email=f"capacity-{index}@example.com", password="TestPassword!9317",
                full_name=f"Capacity User {index}", platform_username=f"capacity_{index}",
                whatsapp_number=f"98765{index:05d}", state="DL", gender="female",
                approval_status="approved", is_active=True,
            )
        payload = {**self.payload, "quantity": 100}
        first = self.client.post("/api/v1/orders/incoming/", payload, format="json", HTTP_X_API_KEY=self.api_key)
        self.assertEqual(first.status_code, 200)
        task = Task.objects.get(order_id=first.data["order"])
        self.assertEqual((task.total_slots, task.remaining_slots), (100, 100))
        self.assertEqual(first.data["task_count"], 1)
        self.assertEqual(first.data["task"]["id"], task.pk)
        self.assertEqual(first.data["task"]["total_slots"], 100)
        retry = self.client.post("/api/v1/orders/incoming/", payload, format="json", HTTP_X_API_KEY=self.api_key)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data["order"], first.data["order"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Task.objects.get(order_id=first.data["order"]).total_slots, 100)

    def test_external_id_cannot_be_reused_for_different_order_details(self):
        self.client.post(
            "/api/v1/orders/incoming/",
            self.payload,
            format="json",
            HTTP_X_API_KEY=self.api_key,
        )
        changed_payload = {**self.payload, "target_link": "https://example.com/other"}

        response = self.client.post(
            "/api/v1/orders/incoming/",
            changed_payload,
            format="json",
            HTTP_X_API_KEY=self.api_key,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.count(), 1)


class AdminManualOrderAPITests(APITestCase):
    endpoint = "/api/v1/admin/orders/create/"

    def setUp(self):
        import uuid
        User = get_user_model()
        self.admin = User.objects.create_user(
            email="manual-admin@example.com", password="TestPassword!9317",
            full_name="Manual Admin", platform_username="manual_admin",
            whatsapp_number="9876500101", state="DL", gender="female", is_staff=True,
        )
        for index in range(3):
            User.objects.create_user(
                email=f"manual-user-{index}@example.com", password="TestPassword!9317",
                full_name=f"Manual User {index}", platform_username=f"manual_user_{index}",
                whatsapp_number=f"98765001{index:02d}", state="DL", gender="female",
                approval_status="approved", is_active=True,
            )
        self.service = Service.objects.create(
            name="Manual Service", price_per_1000=Decimal("12.3456"),
            minimum_order_quantity=1, capacity_percentage=Decimal("100.00"),
            eligible_level="all", business_share_percentage=Decimal("75.00"),
            user_reward_percentage=Decimal("25.00"), order_source=Service.OrderSource.MANUAL,
        )
        self.payload = {
            "service_id": self.service.pk, "target_link": "https://example.com/manual",
            "quantity": 2, "idempotency_key": str(uuid.uuid4()),
        }

    def test_admin_order_uses_service_pricing_and_generates_one_task_pool(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.post(self.endpoint, self.payload, format="json")
        self.assertEqual(response.status_code, 201)
        order = Order.objects.get(pk=response.data["order"])
        self.assertEqual(order.source, Order.Source.MANUAL)
        self.assertIsNone(order.external_order_id)
        self.assertEqual(order.total_order_value, Decimal("0.02469120"))
        task = Task.objects.get(order=order)
        self.assertEqual((task.total_slots, task.remaining_slots), (2, 2))
        self.assertEqual(task.price_per_1000_snapshot, self.service.price_per_1000)
        self.assertEqual(response.data["task_count"], 1)
        self.assertEqual(response.data["task"], {
            "id": task.pk, "status": "available", "total_slots": 2, "remaining_slots": 2,
            "reward_per_approved_claim": str(task.reward_amount),
        })

    def test_same_request_key_returns_existing_order_and_does_not_duplicate_tasks(self):
        self.client.force_authenticate(user=self.admin)
        first = self.client.post(self.endpoint, self.payload, format="json")
        retry = self.client.post(self.endpoint, self.payload, format="json")
        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry.data["order"], first.data["order"])
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(Task.objects.filter(order_id=first.data["order"]).count(), 1)
        self.assertEqual(retry.data["task"]["total_slots"], 2)

    def test_admin_order_list_can_verify_an_uncertain_request_key(self):
        self.client.force_authenticate(user=self.admin)
        created = self.client.post(self.endpoint, self.payload, format="json")
        key = self.payload["idempotency_key"]
        found = self.client.get(f"/api/v1/admin/orders/?idempotency_key={key}")
        missing = self.client.get("/api/v1/admin/orders/?idempotency_key=00000000-0000-0000-0000-000000000001")
        invalid = self.client.get("/api/v1/admin/orders/?idempotency_key=invalid")
        self.assertEqual(created.status_code, 201)
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.data["count"], 1)
        self.assertEqual(found.data["results"][0]["idempotency_key"], key)
        self.assertEqual(found.data["results"][0]["task_count"], 1)
        self.assertEqual(found.data["results"][0]["total_slots"], 2)
        self.assertEqual(missing.data["count"], 0)
        self.assertEqual(invalid.status_code, 400)

    def test_quantity_100_creates_one_task_with_100_slots_and_retry_does_not_duplicate(self):
        self.client.force_authenticate(user=self.admin)
        User = get_user_model()
        for index in range(97):
            User.objects.create_user(
                email=f"manual-capacity-{index}@example.com", password="TestPassword!9317",
                full_name=f"Manual Capacity {index}", platform_username=f"manual_capacity_{index}",
                whatsapp_number=f"98764{index:05d}", state="DL", gender="female",
                approval_status="approved", is_active=True,
            )
        payload = {**self.payload, "quantity": 100}
        first = self.client.post(self.endpoint, payload, format="json")
        self.assertEqual(first.status_code, 201)
        task = Task.objects.get(order_id=first.data["order"])
        self.assertEqual((task.total_slots, task.remaining_slots), (100, 100))
        retry = self.client.post(self.endpoint, payload, format="json")
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(Task.objects.filter(order_id=first.data["order"]).count(), 1)
        self.assertEqual(Task.objects.get(order_id=first.data["order"]).total_slots, 100)

    def test_invalid_service_quantity_and_request_key_reuse_are_rejected(self):
        self.client.force_authenticate(user=self.admin)
        invalid_service = {**self.payload, "service_id": 999999}
        self.assertEqual(self.client.post(self.endpoint, invalid_service, format="json").status_code, 400)
        invalid_quantity = {**self.payload, "quantity": 0}
        self.assertEqual(self.client.post(self.endpoint, invalid_quantity, format="json").status_code, 400)
        self.client.post(self.endpoint, self.payload, format="json")
        changed = {**self.payload, "quantity": 3}
        self.assertEqual(self.client.post(self.endpoint, changed, format="json").status_code, 400)
        self.assertEqual(Order.objects.count(), 1)

    def test_non_admin_cannot_create_manual_order(self):
        user = get_user_model().objects.create_user(
            email="manual-regular@example.com", password="TestPassword!9317",
            full_name="Regular", platform_username="manual_regular",
            whatsapp_number="9876500199", state="DL", gender="female",
        )
        self.client.force_authenticate(user=user)
        self.assertEqual(self.client.post(self.endpoint, self.payload, format="json").status_code, 403)

    def test_order_is_rolled_back_if_task_generation_fails(self):
        from unittest.mock import patch
        from django.core.exceptions import ValidationError
        self.client.force_authenticate(user=self.admin)
        with patch("orders.services.create_tasks_for_order", side_effect=ValidationError("Task generation failed.")):
            response = self.client.post(self.endpoint, self.payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(Order.objects.count(), 0)
        self.assertEqual(Task.objects.count(), 0)
