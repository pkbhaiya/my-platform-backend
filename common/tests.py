from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase
from decimal import Decimal

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from orders.models import Order
from services_catalog.models import Service
from tasks.models import Task, TaskClaim
from wallet.models import Wallet, WalletTransaction

from .models import SiteConfiguration


class SiteConfigurationTests(TestCase):
    def test_minimum_withdrawal_default(self):
        configuration = SiteConfiguration.objects.create()

        self.assertEqual(
            configuration.min_withdrawal_amount,
            Decimal("100.00"),
        )

    def test_database_rejects_nonpositive_withdrawal_minimum(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SiteConfiguration.objects.create(
                    min_withdrawal_amount=Decimal("0.00")
                )


from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase


class AdminSiteConfigurationAPITests(APITestCase):
    endpoint = "/api/v1/admin/platform-settings/"

    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            email="settings-admin@example.com", password="TestPassword!9317",
            full_name="Settings Admin", platform_username="settings_admin",
            whatsapp_number="9876543200", state="DL", gender="female", is_staff=True,
        )
        self.user = User.objects.create_user(
            email="settings-user@example.com", password="TestPassword!9317",
            full_name="Settings User", platform_username="settings_user",
            whatsapp_number="9876543201", state="DL", gender="female",
        )

    def test_admin_can_read_and_update_real_site_configuration(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 200)
        self.assertIn("min_withdrawal_amount", response.data)
        self.assertNotIn("INCOMING_ORDER_API_KEY", response.data)

        response = self.client.patch(
            self.endpoint,
            {"min_withdrawal_amount": "125.00", "approval_title": "Approval review"},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        config = SiteConfiguration.objects.get(pk=1)
        self.assertEqual(config.min_withdrawal_amount, Decimal("125.00"))
        self.assertEqual(config.approval_title, "Approval review")

    def test_non_admin_is_denied(self):
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.get(self.endpoint).status_code, 403)

    def test_non_positive_minimum_is_rejected(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(self.endpoint, {"min_withdrawal_amount": "0"}, format="json")
        self.assertEqual(response.status_code, 400)


class AdminBulkDeleteAPITests(APITestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_user(
            email="bulk-admin@example.com", password="TestPassword!9317",
            full_name="Bulk Admin", platform_username="bulk_admin",
            whatsapp_number="9876543200", state="DL", gender="female", is_staff=True,
        )
        self.user = User.objects.create_user(
            email="bulk-user@example.com", password="TestPassword!9317",
            full_name="Bulk User", platform_username="bulk_user",
            whatsapp_number="9876543201", state="DL", gender="female",
        )
        self.service = Service.objects.create(
            name="Bulk Test Service", price_per_1000=Decimal("100.0000"),
            minimum_order_quantity=1, business_share_percentage=Decimal("75.00"),
            user_reward_percentage=Decimal("25.00"),
        )

    def make_order(self, service=None):
        return Order.objects.create(
            service=service or self.service, target_link="https://example.com/order",
            quantity=1, price_per_1000_snapshot=Decimal("100.0000"),
            fulfillment_cost_per_1000_snapshot=None,
            business_share_percentage_snapshot=Decimal("75.00"),
            user_reward_percentage_snapshot=Decimal("25.00"),
            capacity_percentage_snapshot=Decimal("70.00"), total_order_value=Decimal("0.10000000"),
        )

    def make_task(self, **kwargs):
        return Task.objects.create(target_link="https://example.com/task", **kwargs)

    def test_admin_deletes_unreferenced_order_and_keeps_unselected_records(self):
        self.client.force_authenticate(user=self.admin)
        selected = self.make_order()
        unselected = self.make_order()
        response = self.client.post("/api/v1/admin/orders/bulk-delete/", {"ids": [selected.pk]}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["deleted"], 1)
        self.assertFalse(Order.objects.filter(pk=selected.pk).exists())
        self.assertTrue(Order.objects.filter(pk=unselected.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())

    def test_partial_order_delete_failure_is_reported_without_rolling_back_other_ids(self):
        from unittest.mock import patch

        self.client.force_authenticate(user=self.admin)
        first = self.make_order()
        second = self.make_order()
        real_delete = Order.delete

        def delete_one(order, *args, **kwargs):
            if order.pk == second.pk:
                raise RuntimeError("simulated per-record failure")
            return real_delete(order, *args, **kwargs)

        with patch("common.admin_bulk_delete.Order.delete", autospec=True, side_effect=delete_one), \
                self.assertLogs("common.admin_bulk_delete", level="ERROR") as logs:
            response = self.client.post("/api/v1/admin/orders/bulk-delete/", {"ids": [first.pk, second.pk]}, format="json")
        # The admin gets a generic reason; the real cause is in the server log.
        self.assertIn(f"Order #{second.pk} failed", logs.output[0])
        self.assertIn("simulated per-record failure", logs.output[0])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["deleted"], 1)
        self.assertEqual(response.data["failed"], 1)
        self.assertFalse(Order.objects.filter(pk=first.pk).exists())
        self.assertTrue(Order.objects.filter(pk=second.pk).exists())

    def test_order_with_claimed_task_is_skipped_and_history_is_preserved(self):
        self.client.force_authenticate(user=self.admin)
        order = self.make_order()
        task = self.make_task(order=order)
        TaskClaim.objects.create(task=task, user=self.user)
        response = self.client.post("/api/v1/admin/orders/bulk-delete/", {"ids": [order.pk]}, format="json")
        self.assertEqual(response.data["skipped"], 1)
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())
        self.assertTrue(Task.objects.filter(pk=task.pk).exists())

    def test_order_with_unclaimed_task_is_deleted_together_with_its_task(self):
        self.client.force_authenticate(user=self.admin)
        order = self.make_order()
        task = self.make_task(order=order)
        response = self.client.post("/api/v1/admin/orders/bulk-delete/", {"ids": [order.pk]}, format="json")
        self.assertEqual(response.data["deleted"], 1)
        self.assertFalse(Order.objects.filter(pk=order.pk).exists())
        self.assertFalse(Task.objects.filter(pk=task.pk).exists())

    def test_admin_deletes_selected_standalone_task_only(self):
        self.client.force_authenticate(user=self.admin)
        selected = self.make_task()
        unselected = self.make_task()
        response = self.client.post("/api/v1/admin/tasks/bulk-delete/", {"ids": [selected.pk]}, format="json")
        self.assertEqual(response.data["deleted"], 1)
        self.assertFalse(Task.objects.filter(pk=selected.pk).exists())
        self.assertTrue(Task.objects.filter(pk=unselected.pk).exists())

    def test_task_with_claim_history_is_skipped(self):
        self.client.force_authenticate(user=self.admin)
        claimed = self.make_task()
        TaskClaim.objects.create(task=claimed, user=self.user)
        response = self.client.post("/api/v1/admin/tasks/bulk-delete/", {"ids": [claimed.pk]}, format="json")
        self.assertEqual(response.data["skipped"], 1)
        self.assertTrue(Task.objects.filter(pk=claimed.pk).exists())
        self.assertTrue(TaskClaim.objects.filter(task=claimed).exists())

    def test_unclaimed_order_task_is_deleted_and_its_order_is_kept_but_cancelled(self):
        self.client.force_authenticate(user=self.admin)
        order = self.make_order()
        order_task = self.make_task(order=order)
        preview = self.client.post("/api/v1/admin/tasks/bulk-delete/", {"ids": [order_task.pk], "dry_run": True}, format="json")
        self.assertEqual(preview.data["deleted"], 1)
        self.assertTrue(Task.objects.filter(pk=order_task.pk).exists())
        response = self.client.post("/api/v1/admin/tasks/bulk-delete/", {"ids": [order_task.pk]}, format="json")
        self.assertEqual(response.data["deleted"], 1)
        self.assertFalse(Task.objects.filter(pk=order_task.pk).exists())
        order.refresh_from_db()
        self.assertEqual(order.fulfillment_status, Order.FulfillmentStatus.CANCELLED)
        self.assertEqual(order.availability_status, Order.AvailabilityStatus.UNAVAILABLE)

    def test_wallet_reward_ledger_and_user_are_preserved_when_task_is_skipped(self):
        self.client.force_authenticate(user=self.admin)
        task = self.make_task()
        claim = TaskClaim.objects.create(task=task, user=self.user, status=TaskClaim.Status.APPROVED)
        wallet = Wallet.objects.create(user=self.user)
        transaction_record = WalletTransaction.objects.create(
            wallet=wallet, transaction_type=WalletTransaction.TransactionType.TASK_REWARD,
            direction=WalletTransaction.Direction.CREDIT, amount=Decimal("1.00000000"),
            balance_after=Decimal("1.00000000"), task=task, claim=claim,
            description="Test reward",
        )
        response = self.client.post("/api/v1/admin/tasks/bulk-delete/", {"ids": [task.pk]}, format="json")
        self.assertEqual(response.data["skipped"], 1)
        self.assertTrue(Task.objects.filter(pk=task.pk).exists())
        self.assertTrue(TaskClaim.objects.filter(pk=claim.pk).exists())
        self.assertTrue(WalletTransaction.objects.filter(pk=transaction_record.pk).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.user.pk).exists())

    def test_service_is_archived_when_referenced_and_removed_from_active_services(self):
        self.client.force_authenticate(user=self.admin)
        service = self.service
        order = self.make_order(service)
        response = self.client.post("/api/v1/admin/services/bulk-delete/", {"ids": [service.pk]}, format="json")
        self.assertEqual(response.data["archived"], 1)
        service.refresh_from_db()
        order.refresh_from_db()
        self.assertFalse(service.is_active)
        self.assertEqual(order.service_id, service.pk)
        self.assertTrue(Service.objects.filter(pk=service.pk).exists())

    def test_dry_run_reports_per_record_plan_without_changing_records(self):
        self.client.force_authenticate(user=self.admin)
        order = self.make_order()
        self.make_task(order=order)
        service = order.service
        response = self.client.post(
            "/api/v1/admin/services/bulk-delete/",
            {"ids": [service.pk], "dry_run": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data["dry_run"])
        self.assertEqual(response.data["results"][0]["result"], "archived")
        service.refresh_from_db()
        self.assertTrue(service.is_active)
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())

    def test_archived_service_is_not_listed_or_accepted_for_new_orders(self):
        from orders.testing import make_admin_wallet

        _client, api_key = make_admin_wallet()
        self.client.force_authenticate(user=self.admin)
        service = self.service
        self.make_order(service)
        self.client.post("/api/v1/admin/services/bulk-delete/", {"ids": [service.pk]}, format="json")
        self.client.force_authenticate(user=None)
        listed = self.client.get("/api/v1/services/incoming/", HTTP_X_API_KEY=api_key)
        rejected = self.client.post(
            "/api/v1/orders/incoming/",
            {"service_id": service.pk, "external_order_id": "bulk-delete-new-order", "target_link": "https://example.com/new", "quantity": 1},
            format="json", HTTP_X_API_KEY=api_key,
        )
        self.assertEqual(listed.status_code, 200)
        self.assertNotIn(service.pk, [item["service_id"] for item in listed.data])
        self.assertEqual(rejected.status_code, 400)
        self.assertFalse(Order.objects.filter(external_order_id="bulk-delete-new-order").exists())

    def test_unreferenced_service_is_deleted_and_mixed_ids_report_partial_results(self):
        self.client.force_authenticate(user=self.admin)
        service = self.service
        response = self.client.post("/api/v1/admin/services/bulk-delete/", {"ids": [service.pk, 999999]}, format="json")
        self.assertEqual(response.data["deleted"], 1)
        self.assertEqual(response.data["skipped"], 1)
        self.assertFalse(Service.objects.filter(pk=service.pk).exists())

    def test_empty_duplicate_and_invalid_ids_are_rejected(self):
        self.client.force_authenticate(user=self.admin)
        endpoint = "/api/v1/admin/tasks/bulk-delete/"
        self.assertEqual(self.client.post(endpoint, {"ids": []}, format="json").status_code, 400)
        self.assertEqual(self.client.post(endpoint, {"ids": [1, 1]}, format="json").status_code, 400)
        self.assertEqual(self.client.post(endpoint, {"ids": ["bad"]}, format="json").status_code, 400)

    def test_bulk_delete_requires_admin(self):
        endpoint = "/api/v1/admin/tasks/bulk-delete/"
        self.assertEqual(self.client.post(endpoint, {"ids": [1]}, format="json").status_code, 401)
        self.client.force_authenticate(user=self.user)
        self.assertEqual(self.client.post(endpoint, {"ids": [1]}, format="json").status_code, 403)
