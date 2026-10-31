"""Service form rules: derived business share, target type, editability and snapshots."""

from decimal import Decimal
from uuid import uuid4

from django.contrib.auth import get_user_model
from rest_framework.test import APIClient, APITestCase

from orders.models import Order
from orders.testing import unique_link
from services_catalog.models import Service

User = get_user_model()
SERVICES = "/api/v1/admin/services/"


def make_user(email, **extra):
    fields = dict(
        email=email, password="TestPassword!9317", full_name="Form User",
        platform_username=email.split("@")[0], whatsapp_number="9123400010",
        state="DL", gender="female", approval_status="approved", is_active=True,
    )
    fields.update(extra)
    return User.objects.create_user(**fields)


class ServiceFormMixin:
    def setUp(self):
        self.admin = make_user("form-admin@example.com", is_staff=True)
        self.client.force_authenticate(self.admin)
        # Enough eligible workers that small manual orders fit the capacity.
        for index in range(5):
            make_user(f"form-worker-{index}@example.com")

    def create_service(self, **overrides):
        payload = {
            "name": "Followers", "price_per_1000": "100.0000", "minimum_order_quantity": 1, "max_order_quantity": 1000,
            "capacity_percentage": "100.00", "user_reward_percentage": "25.00", "order_source": "both",
            "target_link_required": True, "is_active": True,
        }
        payload.update(overrides)
        response = self.client.post(SERVICES, payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def order(self, service_id, quantity=2):
        response = self.client.post("/api/v1/admin/orders/create/", {
            "service_id": service_id, "target_link": unique_link(), "quantity": quantity,
            "idempotency_key": str(uuid4()),
        }, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return Order.objects.get(pk=response.data["order"])


class ServiceFormTests(ServiceFormMixin, APITestCase):
    def test_client_business_share_is_ignored_on_create_and_update(self):
        created = self.create_service(user_reward_percentage="20.00", business_share_percentage="10.00")
        self.assertEqual(created["business_share_percentage"], "80.00")
        updated = self.client.patch(f"{SERVICES}{created['service_id']}/", {
            "user_reward_percentage": "35.00", "business_share_percentage": "1.00",
        }, format="json")
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.data["business_share_percentage"], "65.00")
        self.assertEqual(Service.objects.get(pk=created["service_id"]).business_share_percentage, Decimal("65.00"))

    def test_only_business_share_sent_changes_nothing(self):
        created = self.create_service()
        response = self.client.patch(f"{SERVICES}{created['service_id']}/", {"business_share_percentage": "50.00"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["business_share_percentage"], "75.00")

    def test_target_type_defaults_to_post_and_rejects_unknown_values(self):
        created = self.create_service()
        self.assertEqual(created["target_type"], "post")
        bad = self.client.patch(f"{SERVICES}{created['service_id']}/", {"target_type": "story"}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("target_type", bad.data)

    def test_detail_endpoint_returns_every_form_field(self):
        created = self.create_service(target_type="profile", fulfillment_cost_per_1000=None)
        detail = self.client.get(f"{SERVICES}{created['service_id']}/")
        self.assertEqual(detail.status_code, 200)
        for field in ("name", "description", "category", "target_type", "price_per_1000", "fulfillment_cost_per_1000",
                      "minimum_order_quantity", "max_order_quantity", "live_max_order_quantity", "capacity_percentage", "eligible_level", "business_share_percentage",
                      "user_reward_percentage", "order_source", "target_link_required", "is_active"):
            self.assertIn(field, detail.data)
        self.assertEqual(detail.data["target_type"], "profile")
        self.assertIsNone(detail.data["fulfillment_cost_per_1000"])

    def test_max_order_quantity_is_required_and_at_least_the_minimum(self):
        payload = {"name": "No max", "price_per_1000": "10.0000", "minimum_order_quantity": 5, "user_reward_percentage": "25.00"}
        missing = self.client.post(SERVICES, payload, format="json")
        self.assertEqual(missing.status_code, 400)
        self.assertIn("max_order_quantity", missing.data)
        below = self.client.post(SERVICES, {**payload, "max_order_quantity": 4}, format="json")
        self.assertEqual(below.status_code, 400)
        self.assertIn("max_order_quantity", below.data)
        blank = self.client.post(SERVICES, {**payload, "max_order_quantity": 5, "category": "  "}, format="json")
        self.assertEqual(blank.data, {"category": ["Enter a category, for example General."]})
        created = self.create_service(minimum_order_quantity=5, max_order_quantity=5)
        self.assertEqual(created["max_order_quantity"], 5)
        self.assertEqual(created["category"], "General")
        self.assertEqual(self.client.patch(f"{SERVICES}{created['service_id']}/", {"category": " "}, format="json").status_code, 400)
        lowered_min = self.client.patch(f"{SERVICES}{created['service_id']}/", {"minimum_order_quantity": 6}, format="json")
        self.assertEqual(lowered_min.status_code, 400)
        self.assertIn("max_order_quantity", lowered_min.data)

    def test_detail_reports_the_live_capacity_limit(self):
        created = self.create_service(capacity_percentage="60.00")
        detail = self.client.get(f"{SERVICES}{created['service_id']}/").data
        # Five approved workers x 60% = 3 units today, whatever the stable maximum.
        self.assertEqual((detail["max_order_quantity"], detail["live_max_order_quantity"]), (1000, 3))

    def test_null_cost_is_accepted_and_the_text_null_is_a_field_error(self):
        created = self.create_service(has_fulfillment_cost=True, fulfillment_cost_per_1000="3.0000")
        url = f"{SERVICES}{created['service_id']}/"
        cleared = self.client.patch(url, {"fulfillment_cost_per_1000": None}, format="json")
        self.assertEqual(cleared.status_code, 200)
        self.assertIsNone(cleared.data["fulfillment_cost_per_1000"])
        text_null = self.client.patch(url, {"fulfillment_cost_per_1000": "null"}, format="json")
        self.assertEqual(text_null.status_code, 400)
        self.assertIn("fulfillment_cost_per_1000", text_null.data)

    def test_every_editable_field_can_be_changed_with_patch(self):
        created = self.create_service()
        url = f"{SERVICES}{created['service_id']}/"
        changes = {
            "name": "Renamed", "description": "New description", "category": "Instagram", "target_type": "video",
            "max_order_quantity": 500,
            "price_per_1000": "150.5000", "has_fulfillment_cost": True, "fulfillment_cost_per_1000": "12.2500", "minimum_order_quantity": 25,
            "capacity_percentage": "40.00", "user_reward_percentage": "15.00", "order_source": "manual",
            "target_link_required": False, "is_active": False,
        }
        for field, value in changes.items():
            response = self.client.patch(url, {field: value}, format="json")
            self.assertEqual(response.status_code, 200, (field, response.data))
            self.assertEqual(response.data[field], value, field)
        self.assertEqual(self.client.get(url).data["business_share_percentage"], "85.00")

    def test_edits_affect_only_new_orders(self):
        created = self.create_service(target_type="post", has_fulfillment_cost=True, fulfillment_cost_per_1000="10.0000")
        service_id = created["service_id"]
        old = self.order(service_id)
        old_task = old.tasks.get()
        before = {field: getattr(old, field) for field in (
            "price_per_1000_snapshot", "fulfillment_cost_per_1000_snapshot", "has_fulfillment_cost_snapshot", "business_share_percentage_snapshot",
            "user_reward_percentage_snapshot", "capacity_percentage_snapshot", "total_order_value", "target_type_snapshot",
        )}
        response = self.client.patch(f"{SERVICES}{service_id}/", {
            "price_per_1000": "200.0000", "fulfillment_cost_per_1000": "20.0000", "user_reward_percentage": "40.00",
            "capacity_percentage": "90.00", "target_type": "profile",
        }, format="json")
        self.assertEqual(response.status_code, 200, response.data)

        old.refresh_from_db()
        old_task.refresh_from_db()
        self.assertEqual({field: getattr(old, field) for field in before}, before)
        self.assertEqual(old_task.reward_amount, Decimal("0.02500000"))
        self.assertEqual(old_task.target_type, "post")

        new = self.order(service_id)
        self.assertEqual(new.price_per_1000_snapshot, Decimal("200.0000"))
        self.assertEqual(new.business_share_percentage_snapshot, Decimal("60.00"))
        self.assertEqual(new.target_type_snapshot, "profile")
        self.assertEqual(new.tasks.get().reward_amount, Decimal("0.08000000"))

    def test_claim_exposes_the_order_target_type(self):
        created = self.create_service(target_type="survey")
        order = self.order(created["service_id"], quantity=1)
        worker = User.objects.get(email="form-worker-0@example.com")
        client = APIClient()
        client.force_authenticate(worker)
        task_id = order.tasks.get().pk
        self.assertEqual(client.get("/api/v1/tasks/available/").data["results"][0]["target_type"], "survey")
        self.assertEqual(client.post(f"/api/v1/tasks/{task_id}/claim/").status_code, 200)
        self.assertEqual(client.get("/api/v1/tasks/my/").data[0]["target_type"], "survey")


class FulfillmentCostFlagTests(ServiceFormMixin, APITestCase):
    def test_a_cost_needs_the_flag_and_the_flag_is_snapshotted(self):
        refused = self.client.post(SERVICES, {"name": "X", "price_per_1000": "10.0000", "max_order_quantity": 100, "fulfillment_cost_per_1000": "1.0000"}, format="json")
        self.assertEqual(refused.status_code, 400)
        self.assertIn("fulfillment_cost_per_1000", refused.data)
        free = self.create_service()
        self.assertFalse(free["has_fulfillment_cost"])
        order = self.order(free["service_id"])
        self.assertFalse(order.has_fulfillment_cost_snapshot)
        self.assertEqual(order.fulfillment_cost_for(2), Decimal("0"))
        costly = self.create_service(has_fulfillment_cost=True)
        unknown = self.order(costly["service_id"])
        self.assertTrue(unknown.has_fulfillment_cost_snapshot)
        self.assertIsNone(unknown.fulfillment_cost_for(2))

    def test_the_database_refuses_a_cost_on_a_service_without_one(self):
        from django.db import IntegrityError, transaction
        service = Service.objects.create(name="Free", price_per_1000=Decimal("10.0000"))
        with self.assertRaises(IntegrityError), transaction.atomic():
            Service.objects.filter(pk=service.pk).update(fulfillment_cost_per_1000=Decimal("1.0000"))

    def test_dashboard_profit_uses_zero_cost_for_services_without_one(self):
        order = self.order(self.create_service()["service_id"])
        Order.objects.filter(pk=order.pk).update(fulfillment_status=Order.FulfillmentStatus.COMPLETED)
        current = self.client.get("/api/v1/admin/dashboard/stats/").data["financials"]["current"]
        self.assertTrue(current["profit_available"])
        self.assertEqual(Decimal(current["profit"]), Decimal("0"))
