"""Regression tests for fixes from the October 2026 code review."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from services_catalog.models import Service

User = get_user_model()


class AdminServiceDetailTests(APITestCase):
    def setUp(self):
        admin = User.objects.create_user(
            email="service-admin@example.com", password="TestPassword!9317", full_name="Admin",
            platform_username="service_admin", whatsapp_number="9123400002", state="DL",
            gender="female", approval_status="approved", is_staff=True,
        )
        self.client.force_authenticate(admin)

    def test_missing_service_is_404(self):
        self.assertEqual(self.client.get("/api/v1/admin/services/999999/").status_code, 404)
        self.assertEqual(
            self.client.patch("/api/v1/admin/services/999999/", {"name": "x"}, format="json").status_code, 404,
        )

    def test_validation_errors_are_reported_per_field(self):
        service = Service.objects.create(name="Valid", price_per_1000=Decimal("10.0000"))
        response = self.client.patch(
            f"/api/v1/admin/services/{service.pk}/", {"capacity_percentage": "100.01"}, format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("capacity_percentage", response.data)
