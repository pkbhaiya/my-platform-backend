from decimal import Decimal
from types import SimpleNamespace

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase
from django.test import TestCase

from .models import Service
from .calculations import (
    calculate_business_allocation,
    calculate_max_order_quantity,
    calculate_order_value,
    calculate_price_per_unit,
    calculate_user_reward,
)


class ServiceCalculationTests(SimpleTestCase):
    def setUp(self):
        # A sample service for calculation tests.
        self.service = SimpleNamespace(
            price_per_1000=Decimal("1000.0000"),
            capacity_percentage=Decimal("70.00"),
            business_share_percentage=Decimal("75.00"),
            user_reward_percentage=Decimal("25.00"),
        )

    def test_price_per_unit(self):
        result = calculate_price_per_unit(self.service)
        self.assertEqual(result, Decimal("1"))

    def test_order_value(self):
        result = calculate_order_value(self.service, 100)
        self.assertEqual(result, Decimal("100"))

    def test_user_reward(self):
        result = calculate_user_reward(self.service, 100)
        self.assertEqual(result, Decimal("25"))

    def test_business_allocation(self):
        result = calculate_business_allocation(self.service, 100)
        self.assertEqual(result, Decimal("75"))

    def test_max_order_quantity(self):
        result = calculate_max_order_quantity(self.service, 10000)
        self.assertEqual(result, 7000)

    def test_capacity_rounds_down(self):
        result = calculate_max_order_quantity(self.service, 11)
        self.assertEqual(result, 7)

    def test_negative_quantity_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_order_value(self.service, -1)

    def test_negative_user_count_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_max_order_quantity(self.service, -1)


class ServiceValidationTests(TestCase):
    def make_service(self, **overrides):
        values = {
            "name": "Validation Test",
            "price_per_1000": Decimal("10.0000"),
            "minimum_order_quantity": 1,
            "business_share_percentage": Decimal("75.00"),
            "user_reward_percentage": Decimal("25.00"),
        }
        values.update(overrides)
        return Service(**values)

    def test_business_share_is_derived_from_user_reward_on_save(self):
        # A conflicting business share is ignored: it is always 100 - reward.
        service = self.make_service(user_reward_percentage=Decimal("24.99"))
        service.save()
        service.refresh_from_db()
        self.assertEqual(service.business_share_percentage, Decimal("75.01"))

    def test_percentage_outside_range_is_rejected(self):
        service = self.make_service(capacity_percentage=Decimal("100.01"))

        with self.assertRaises(ValidationError):
            service.save()

    def test_database_rejects_percentage_invariant_bypass(self):
        service = self.make_service()
        service.save()

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Service.objects.filter(pk=service.pk).update(
                    user_reward_percentage=Decimal("30.00")
                )


from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase
from orders.models import Order
from .models import Service


class AdminServiceAPITests(APITestCase):
    endpoint = "/api/v1/admin/services/"

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            email="service-admin@example.com", password="TestPassword!9317",
            full_name="Service Admin", platform_username="service_admin",
            whatsapp_number="9876543210", state="DL", gender="female",
            is_staff=True,
        )
        self.user = User.objects.create_user(
            email="service-user@example.com", password="TestPassword!9317",
            full_name="Service User", platform_username="service_user",
            whatsapp_number="9876543211", state="DL", gender="female",
        )
        self.service = Service.objects.create(
            name="Admin API Service", price_per_1000=Decimal("10.0000"),
            minimum_order_quantity=1, capacity_percentage=Decimal("100.00"),
            business_share_percentage=Decimal("75.00"),
            user_reward_percentage=Decimal("25.00"), order_source="both",
        )

    def test_admin_can_list_and_search_services(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(self.endpoint, {"search": "Admin API"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        self.assertEqual(response.data["results"][0]["service_id"], self.service.pk)

    def test_regular_user_cannot_list_services(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 403)

    def test_admin_can_edit_service_without_rewriting_order_snapshots(self):
        order = Order.objects.create(
            service=self.service, source="api", external_order_id="snapshot-1",
            target_link="https://example.com/item", quantity=1,
            price_per_1000_snapshot=Decimal("10.0000"),
            business_share_percentage_snapshot=Decimal("75.00"),
            user_reward_percentage_snapshot=Decimal("25.00"),
            capacity_percentage_snapshot=Decimal("100.00"),
            eligible_user_count_at_acceptance=1, capacity_limit_at_acceptance=1,
            total_order_value=Decimal("0.01000000"),
        )
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(
            f"{self.endpoint}{self.service.pk}/",
            {"price_per_1000": "20.0000", "user_reward_percentage": "30.00", "business_share_percentage": "70.00", "is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.service.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.service.price_per_1000, Decimal("20.0000"))
        self.assertFalse(self.service.is_active)
        self.assertEqual(order.price_per_1000_snapshot, Decimal("10.0000"))
        self.assertEqual(order.total_order_value, Decimal("0.01000000"))

    def test_reward_change_alone_rederives_business_share(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(
            f"{self.endpoint}{self.service.pk}/",
            {"user_reward_percentage": "30.00"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.service.refresh_from_db()
        self.assertEqual(self.service.user_reward_percentage, Decimal("30.00"))
        self.assertEqual(self.service.business_share_percentage, Decimal("70.00"))
