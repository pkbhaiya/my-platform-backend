"""Sandbox orders (orders.sandbox): placed with a sandbox wallet's key, charged, never given a Task."""

import re
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from orders.models import AdminWallet, AdminWalletTransaction, Order
from orders.reconciliation import reconcile_admin_wallets
from orders.sandbox import advance_sandbox_orders
from orders.services import create_provider_order
from orders.test_provider_api import PLENTY, URL, make_service
from orders.testing import make_admin_wallet, unique_link
from tasks.models import Task
from tasks.test_claim_lifecycle import make_user

Type = AdminWalletTransaction.Type


def make_sandbox_wallet(balance="100.00"):
    wallet, key = make_admin_wallet("Sandbox", balance=balance)
    AdminWallet.objects.filter(pk=wallet.pk).update(is_sandbox=True)
    wallet.refresh_from_db()
    return wallet, key


# Plain-HTTP test requests, as in test_provider_api.
@override_settings(PROVIDER_API_REQUIRE_HTTPS=False)
class SandboxTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.sandbox, self.key = make_sandbox_wallet()
        self.service = make_service()  # ₹200 per 1,000, quantity 10-500
        self.api = APIClient()

    def form(self, key=None, **data):
        response = self.api.post(URL, urlencode({"key": key or self.key, **data}), content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def add(self, quantity=50, link=None, key=None):
        with PLENTY:
            return self.form(key=key, action="add", service=self.service.pk, link=link or unique_link(), quantity=quantity)

    def status(self, order_id):
        return self.form(action="status", order=order_id)

    def balance(self):
        return AdminWallet.objects.get(pk=self.sandbox.pk).balance

    def superuser_client(self):
        wallet, _ = make_admin_wallet("Boss", superuser=True)
        client = APIClient()
        client.force_authenticate(wallet.admin)
        return client


class SandboxOrderCreationTests(SandboxTestCase):
    def test_add_charges_the_sandbox_wallet_and_creates_no_task(self):
        body = self.add(quantity=50)  # 200 x 50 / 1000 = 10.00
        order = Order.objects.get(pk=body["order"])
        self.assertEqual(body, {"order": order.pk})
        self.assertTrue(order.is_sandbox)
        self.assertEqual(order.admin_wallet_id, self.sandbox.pk)
        self.assertEqual(order.charge_amount, Decimal("10.00"))
        self.assertFalse(Task.objects.exists())
        self.assertEqual(self.balance(), Decimal("90.00"))
        self.assertEqual(AdminWalletTransaction.objects.filter(order=order, transaction_type=Type.ORDER_CHARGE).count(), 1)
        self.assertEqual(self.status(order.pk), {"charge": "10.00", "start_count": "0", "status": "Pending", "remains": "50", "currency": "INR"})
        self.assertEqual(self.form(action="balance"), {"balance": "90.00", "currency": "INR"})

    def test_add_is_validated_exactly_like_a_real_order(self):
        link = unique_link()
        self.add(quantity=50, link=link)
        cases = [
            ({"quantity": 5}, "Minimum order quantity is 10."),
            ({"quantity": 501}, "Maximum order quantity is 500."),
            ({"link": "javascript:alert(1)"}, "Enter a valid target URL."),
            ({"link": link}, "You have an active order with this link. Please wait until it is completed."),
            ({"quantity": 460}, "Not enough funds on balance"),  # 92.00 > 90.00
        ]
        for extra, message in cases:
            with self.subTest(message):
                self.assertEqual(self.add(**{"quantity": 50, **extra}), {"error": message})
        # Capacity is checked too (no approved workers here).
        refused = self.form(action="add", service=self.service.pk, link=unique_link(), quantity=50)
        self.assertEqual(refused, {"error": "Order quantity exceeds the service capacity. Maximum allowed quantity is 0."})
        self.assertEqual(Order.objects.count(), 1)
        self.assertEqual(self.balance(), Decimal("90.00"))

    def test_real_and_sandbox_orders_never_block_each_other_on_a_link(self):
        real, real_key = make_admin_wallet("Real")
        link = unique_link()
        self.assertIn("order", self.add(link=link))
        real_order = self.add(link=link, key=real_key)
        self.assertIn("order", real_order)
        self.assertEqual(Task.objects.filter(order_id=real_order["order"]).count(), 1)
        # Within the sandbox the duplicate rule still applies.
        self.assertEqual(self.add(link=link), {"error": "You have an active order with this link. Please wait until it is completed."})

    def test_workers_never_see_sandbox_orders(self):
        _, real_key = make_admin_wallet("Real")
        sandbox_order = self.add()["order"]
        real_order = self.add(key=real_key)["order"]
        worker = APIClient()
        worker.force_authenticate(make_user(1))
        listed = worker.get("/api/v1/tasks/available/").json()["results"]
        self.assertEqual([task["id"] for task in listed], list(Task.objects.filter(order_id=real_order).values_list("pk", flat=True)))
        self.assertFalse(Task.objects.filter(order_id=sandbox_order).exists())

    def test_no_new_order_alert_for_sandbox_orders(self):
        _, real_key = make_admin_wallet("Real")
        with patch("orders.services.notify_new_order") as notify:
            self.add()
            notify.assert_not_called()
            self.add(key=real_key)
            notify.assert_called_once()

    def test_x_api_key_endpoint_with_the_sandbox_key_also_creates_no_task(self):
        payload = {"service_id": self.service.pk, "external_order_id": "pp-1", "target_link": unique_link(), "quantity": 20}
        with PLENTY:
            first = self.api.post("/api/v1/orders/incoming/", payload, format="json", HTTP_X_API_KEY=self.key)
            retry = self.api.post("/api/v1/orders/incoming/", payload, format="json", HTTP_X_API_KEY=self.key)
        self.assertEqual((first.status_code, retry.status_code), (200, 200))
        self.assertEqual(first.json()["order"], retry.json()["order"])
        self.assertEqual((first.json()["task_count"], first.json()["task"]), (0, None))
        self.assertTrue(Order.objects.get(pk=first.json()["order"]).is_sandbox)
        self.assertFalse(Task.objects.exists())


class SandboxStatusProgressionTests(SandboxTestCase):
    def order(self, quantity):
        order = Order.objects.get(pk=self.add(quantity=quantity)["order"])
        return order, order.created_at

    def test_pending_then_in_progress_after_one_minute_then_completed_after_three(self):
        order, created = self.order(50)
        self.assertEqual(advance_sandbox_orders(now=created + timedelta(seconds=30)), 0)
        self.assertEqual(self.status(order.pk)["status"], "Pending")
        self.assertEqual(advance_sandbox_orders(now=created + timedelta(minutes=1)), 1)
        self.assertEqual(self.status(order.pk), {"charge": "10.00", "start_count": "0", "status": "In progress", "remains": "50", "currency": "INR"})
        self.assertEqual(advance_sandbox_orders(now=created + timedelta(minutes=2)), 0)
        self.assertEqual(advance_sandbox_orders(now=created + timedelta(minutes=3)), 1)
        self.assertEqual(self.status(order.pk), {"charge": "10.00", "start_count": "0", "status": "Completed", "remains": "0", "currency": "INR"})
        self.assertEqual(advance_sandbox_orders(now=created + timedelta(minutes=10)), 0)
        # Completed: nothing is refunded.
        self.assertEqual(self.balance(), Decimal("90.00"))
        self.assertFalse(AdminWalletTransaction.objects.filter(transaction_type=Type.REFUND).exists())

    def test_odd_quantity_ends_partial_with_one_unit_refunded_once(self):
        order, created = self.order(51)  # 200 x 51 / 1000 = 10.20
        self.assertEqual(self.balance(), Decimal("89.80"))
        # Overdue (the timer was not running): straight to its final status.
        self.assertEqual(advance_sandbox_orders(now=created + timedelta(minutes=5)), 1)
        self.assertEqual(self.status(order.pk), {"charge": "10.20", "start_count": "0", "status": "Partial", "remains": "1", "currency": "INR"})
        refund = AdminWalletTransaction.objects.get(transaction_type=Type.REFUND)
        # 10.20 x 1 / 51, rounded down to the paisa, as for every Partial API order.
        self.assertEqual((refund.order_id, refund.amount), (order.pk, Decimal("0.20")))
        self.assertEqual(self.balance(), Decimal("90.00"))
        advance_sandbox_orders(now=created + timedelta(minutes=6))
        self.assertEqual(AdminWalletTransaction.objects.filter(transaction_type=Type.REFUND).count(), 1)

    def test_multiple_status_and_reconciliation_after_both_endings(self):
        partial, created = self.order(51)
        completed, _ = self.order(50)
        advance_sandbox_orders(now=timezone.now() + timedelta(minutes=4))
        body = self.form(action="status", orders=f"{partial.pk},{completed.pk},999999")
        self.assertEqual(body[str(partial.pk)]["status"], "Partial")
        self.assertEqual(body[str(partial.pk)]["remains"], "1")
        self.assertEqual(body[str(completed.pk)]["status"], "Completed")
        self.assertEqual(body["999999"], {"error": "Incorrect order ID"})
        _, mismatches, _ = reconcile_admin_wallets()
        self.assertEqual(mismatches, [])

    def test_expire_claims_and_the_sandbox_command_move_orders_on(self):
        first, _ = self.order(50)
        Order.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(minutes=5))
        out = StringIO()
        call_command("expire_claims", stdout=out)
        self.assertIn("Moved 1 sandbox order(s) on.", out.getvalue())
        self.assertEqual(Order.objects.get(pk=first.pk).fulfillment_status, Order.FulfillmentStatus.COMPLETED)

        second, _ = self.order(50)
        Order.objects.filter(pk=second.pk).update(created_at=timezone.now() - timedelta(seconds=90))
        out = StringIO()
        call_command("advance_sandbox_orders", stdout=out)
        self.assertIn("Moved 1 sandbox order(s) on.", out.getvalue())
        self.assertEqual(Order.objects.get(pk=second.pk).fulfillment_status, Order.FulfillmentStatus.IN_PROGRESS)

    def test_real_orders_are_never_moved_by_the_sandbox_run(self):
        real, _ = make_admin_wallet("Real")
        with PLENTY:
            order = create_provider_order(admin_wallet=real, service_id=self.service.pk, target_link=unique_link(), quantity=51)
        self.assertEqual(advance_sandbox_orders(now=timezone.now() + timedelta(minutes=10)), 0)
        self.assertEqual(Order.objects.get(pk=order.pk).fulfillment_status, Order.FulfillmentStatus.ACCEPTED)


class SandboxExcludedFromAnalyticsTests(SandboxTestCase):
    def test_left_out_of_analytics_dashboards_and_revenue(self):
        _, real_key = make_admin_wallet("Real")
        real_id = self.add(quantity=100, key=real_key)["order"]  # value 20.00, still open
        sandbox_id = self.add(quantity=50)["order"]
        # Completed, so it would count as revenue if it were included.
        advance_sandbox_orders(now=timezone.now() + timedelta(minutes=4))
        admin = self.superuser_client()

        summary = admin.get("/api/v1/admin/analytics/").json()
        self.assertEqual((summary["closed_orders"], summary["open_orders"]), (0, 1))
        self.assertEqual(summary["recognized_revenue"], "0.00000000")
        self.assertEqual(summary["open_order_value"], "20.00000000")

        orders = admin.get("/api/v1/admin/analytics/orders/").json()
        self.assertEqual([row["id"] for row in orders["results"]], [real_id])
        self.assertEqual(admin.get(f"/api/v1/admin/analytics/orders/?admin_wallet={self.sandbox.pk}").json()["count"], 0)
        self.assertEqual(admin.get(f"/api/v1/admin/analytics/orders/{sandbox_id}/").status_code, 404)

        stats = admin.get("/api/v1/admin/dashboard/stats/").json()
        self.assertEqual((stats["orders"]["total"], stats["orders"]["today"], stats["orders"]["total_quantity"]), (1, 1, 100))
        self.assertEqual(stats["financials"]["current"]["order_value"], "20.00000000")
        self.assertEqual(stats["financials"]["current"]["recognized_revenue"], "0.00000000")
        self.assertEqual(stats["tasks"]["total"], 1)

        # The Orders page still lists it, marked as sandbox.
        listed = {row["id"]: row for row in admin.get("/api/v1/admin/orders/").json()["results"]}
        self.assertEqual((listed[sandbox_id]["is_sandbox"], listed[real_id]["is_sandbox"]), (True, False))
        self.assertEqual((listed[sandbox_id]["delivered"], listed[sandbox_id]["remains"], listed[sandbox_id]["task_id"]), (50, 0, None))


class SandboxAdminWalletTests(SandboxTestCase):
    def test_wallet_rows_mark_the_sandbox_wallet(self):
        rows = {row["id"]: row for row in self.superuser_client().get("/api/v1/admin/admin-wallets/").json()["results"]}
        self.assertTrue(rows[self.sandbox.pk]["is_sandbox"])
        self.assertEqual([row["is_sandbox"] for wallet_id, row in rows.items() if wallet_id != self.sandbox.pk], [False])

    def test_superuser_revokes_the_sandbox_key(self):
        admin = self.superuser_client()
        response = admin.post(f"/api/v1/admin/admin-wallets/{self.sandbox.pk}/revoke-sandbox-key/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store, no-cache, must-revalidate, private")
        self.assertEqual((response.json()["is_sandbox"], response.json()["api_key"]["configured"]), (True, False))
        self.assertEqual(self.form(action="balance"), {"error": "Invalid API key"})
        # Again: nothing left to revoke, same answer.
        self.assertEqual(admin.post(f"/api/v1/admin/admin-wallets/{self.sandbox.pk}/revoke-sandbox-key/").status_code, 200)

    def test_only_sandbox_wallets_and_only_superusers(self):
        real, real_key = make_admin_wallet("Real")
        refused = self.superuser_client().post(f"/api/v1/admin/admin-wallets/{real.pk}/revoke-sandbox-key/")
        self.assertEqual((refused.status_code, refused.json()), (400, {"detail": ["Only a sandbox wallet's key can be revoked here."]}))
        self.assertEqual(self.form(key=real_key, action="balance")["currency"], "INR")
        staff = APIClient()
        staff.force_authenticate(real.admin)
        self.assertEqual(staff.post(f"/api/v1/admin/admin-wallets/{self.sandbox.pk}/revoke-sandbox-key/").status_code, 403)
        self.assertEqual(self.form(action="balance")["currency"], "INR")


@override_settings(PROVIDER_API_REQUIRE_HTTPS=False)
class CreateSandboxAccountCommandTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.api = APIClient()

    def run_command(self, *args):
        out = StringIO()
        call_command("create_sandbox_account", *args, stdout=out)
        match = re.search(r"API key \(shown once, not stored\): (\S+)", out.getvalue())
        return out.getvalue(), match.group(1) if match else None

    def balance(self, key):
        return self.api.post(URL, urlencode({"key": key, "action": "balance"}), content_type="application/x-www-form-urlencoded").json()

    def test_creates_an_api_only_staff_account_with_a_funded_sandbox_wallet(self):
        _, key = self.run_command()
        user = get_user_model().objects.get(email="perfectpanel-test@pythonsmm.invalid")
        self.assertEqual((user.is_staff, user.is_superuser, user.is_active), (True, False, True))
        self.assertFalse(user.has_usable_password())
        wallet = user.admin_wallet
        self.assertTrue(wallet.is_sandbox)
        funds = AdminWalletTransaction.objects.get(wallet=wallet)
        self.assertEqual((funds.transaction_type, funds.amount, funds.reference), (Type.ADD_FUNDS, Decimal("100.00"), "PP support test"))
        self.assertEqual(self.balance(key), {"balance": "100.00", "currency": "INR"})
        # The key is the only way in: no password ever signs in to the admin panel.
        for password in ("", "perfectpanel-test", "!"):
            login = self.api.post("/api/v1/auth/login/", {"email": user.email, "password": password}, format="json")
            self.assertNotEqual(login.status_code, 200)
            self.assertNotIn("access", login.json())

    def test_running_again_neither_funds_twice_nor_replaces_the_key_unless_asked(self):
        _, key = self.run_command()
        output, again = self.run_command()
        self.assertIsNone(again)
        self.assertIn("keeps its key", output)
        self.assertEqual(AdminWalletTransaction.objects.filter(transaction_type=Type.ADD_FUNDS).count(), 1)
        self.assertEqual(self.balance(key)["balance"], "100.00")
        _, new_key = self.run_command("--new-key")
        self.assertNotEqual(new_key, key)
        self.assertEqual(self.balance(key), {"error": "Invalid API key"})
        self.assertEqual(self.balance(new_key)["balance"], "100.00")
