"""Phase 11 (revised): one wallet and one personal API key per admin, paying for API orders."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from threading import Barrier
from urllib.parse import urlencode
from unittest.mock import patch

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import User, UserChangeLog
from common.test_notifications import CONFIGURED, sent_texts, telegram_ok
from orders.admin_wallets import (
    charge_for, ensure_admin_wallet, flush_refused_order_alerts, record_adjustment, refund_api_order, refund_due,
)
from orders.api_keys import authenticate_admin_wallet, hash_api_key
from orders.models import AdminWallet, AdminWalletTransaction, Order
from orders.services import create_admin_order, create_provider_order
from orders.test_provider_api import PLENTY, URL, make_service
from orders.testing import unique_link, make_admin_wallet
from tasks.services import approve_submitted_task, cancel_task, expire_old_orders, submit_task_result
from tasks.test_claim_lifecycle import make_user, opened
from wallet.models import Wallet, WalletTransaction
from wallet.reconciliation import reconcile_wallets

Type = AdminWalletTransaction.Type
NOT_ENOUGH = {"error": "Not enough funds on balance"}
INVALID = {"error": "Invalid API key"}
MY = "/api/v1/admin/my-wallet/"
ALL = "/api/v1/admin/admin-wallets/"


@override_settings(PROVIDER_API_REQUIRE_HTTPS=False)
class AdminWalletTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.workers = iter(make_user(index) for index in range(1, 60))
        self.service = make_service()  # ₹200 per 1,000, 10 to 500 units
        self.alpha, self.alpha_key = make_admin_wallet("Alpha", balance="100.00")
        self.owner = self.alpha.admin
        self.superuser = make_user(90, is_staff=True, is_superuser=True)
        self.api = APIClient()
        self.me = APIClient()
        self.me.force_authenticate(self.owner)
        self.root = APIClient()
        self.root.force_authenticate(self.superuser)

    def v2(self, key, **data):
        response = self.api.post(URL, urlencode({"key": key, **data}), content_type="application/x-www-form-urlencoded")
        self.assertEqual(response.status_code, 200)
        return response.json()

    def add(self, key=None, quantity=50, service=None):
        with PLENTY:
            return self.v2(key or self.alpha_key, action="add", service=(service or self.service).pk,
                           link=unique_link(), quantity=quantity)

    def order(self, wallet=None, quantity=50, service=None):
        with PLENTY:
            return create_provider_order(admin_wallet=wallet or self.alpha, service_id=(service or self.service).pk,
                                         target_link=unique_link(), quantity=quantity)

    def balance(self, wallet=None):
        return AdminWallet.objects.get(pk=(wallet or self.alpha).pk).balance

    def rows(self, kind, wallet=None):
        return AdminWalletTransaction.objects.filter(wallet=wallet or self.alpha, transaction_type=kind)

    def deliver(self, order, count):
        task = order.tasks.get()
        for _ in range(count):
            worker = next(self.workers)
            opened(task, worker)
            submit_task_result(task_id=task.pk, user=worker, result="work_done")
            approve_submitted_task(task_id=task.claims.get(user=worker).pk, reviewer=self.superuser)
        return task

    def mismatches(self):
        with self.captureOnCommitCallbacks(execute=True):
            run = reconcile_wallets()
        return sorted(item["check"] for item in run.details["mismatches"] if item["check"].startswith(("api_", "admin_wallet")))


class WalletPerAdminTests(AdminWalletTestCase):
    def test_every_staff_user_has_exactly_one_wallet_and_workers_have_none(self):
        admin = make_user(80, is_staff=True)
        self.assertEqual(AdminWallet.objects.filter(admin=admin).count(), 1)
        wallet = admin.admin_wallet
        self.assertEqual((wallet.balance, wallet.api_key_hash, wallet.api_key_prefix), (Decimal("0.00"), None, ""))
        worker = make_user(81)
        self.assertFalse(AdminWallet.objects.filter(admin=worker).exists())
        # Becoming staff later creates it; saving again never adds a second one.
        worker.is_staff = True
        worker.save()
        worker.save()
        self.assertEqual(AdminWallet.objects.filter(admin=worker).count(), 1)
        self.assertEqual(ensure_admin_wallet(worker).pk, worker.admin_wallet.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AdminWallet.objects.create(admin=worker)
        with self.assertRaisesMessage(ValidationError, "staff accounts only"):
            ensure_admin_wallet(make_user(82))

    def test_a_wallet_without_a_key_cannot_be_used_until_one_is_generated(self):
        admin = make_user(83, is_staff=True)
        self.assertIsNone(authenticate_admin_wallet(""))
        client = APIClient()
        client.force_authenticate(admin)
        response = client.post(f"{MY}regenerate-key/")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.v2(response.data["api_key"], action="balance"), {"balance": "0.00", "currency": "INR"})

    def test_worker_wallets_are_never_touched(self):
        before = (Wallet.objects.count(), WalletTransaction.objects.count())
        order = self.order(quantity=20)
        cancel_task(task_id=order.tasks.get().pk, reviewer=self.superuser, reason="broken_link")
        self.me.post(f"{MY}add-funds/", {"amount": "5.00", "reference": "UTR-W"}, format="json")
        self.assertEqual((Wallet.objects.count(), WalletTransaction.objects.count()), before)


class ChargeTests(AdminWalletTestCase):
    def test_charge_math(self):
        self.assertEqual(charge_for(Decimal("200.0000") * 500 / 1000), Decimal("100.00"))
        self.assertEqual(charge_for(Decimal("0.5000") * 10 / 1000), Decimal("0.01"))  # 0.005 rounds half up
        self.assertEqual(charge_for(Decimal("12.3456") * 7 / 1000), Decimal("0.09"))  # 0.0864192
        self.assertEqual(charge_for(Decimal("0.4000") * 10 / 1000), Decimal("0.00"))

    def test_an_order_is_charged_once_at_creation_and_status_reports_the_same_amount(self):
        body = self.add(quantity=50)
        order = Order.objects.get(pk=body["order"])
        self.assertEqual((order.admin_wallet_id, order.charge_amount), (self.alpha.pk, Decimal("10.00")))
        self.assertEqual(self.balance(), Decimal("90.00"))
        charge = self.rows(Type.ORDER_CHARGE).get()
        self.assertEqual((charge.direction, charge.amount, charge.balance_after, charge.charge_order_id),
                         ("debit", Decimal("10.00"), Decimal("90.00"), order.pk))
        self.assertEqual(self.v2(self.alpha_key, action="status", order=order.pk)["charge"], "10.00")
        self.assertEqual(self.v2(self.alpha_key, action="balance"), {"balance": "90.00", "currency": "INR"})

    def test_rate_200_x_500_costs_100_and_empties_the_balance(self):
        order = Order.objects.get(pk=self.add(quantity=500)["order"])
        self.assertEqual(order.charge_amount, Decimal("100.00"))
        self.assertEqual(self.balance(), Decimal("0.00"))
        self.assertEqual(self.add(quantity=10), NOT_ENOUGH)

    def test_half_a_paisa_is_charged_as_one_paisa(self):
        cheap = make_service(name="Cheap", price_per_1000=Decimal("0.5000"), minimum_order_quantity=1)
        order = Order.objects.get(pk=self.add(quantity=10, service=cheap)["order"])
        self.assertEqual(order.charge_amount, Decimal("0.01"))
        self.assertEqual(self.v2(self.alpha_key, action="status", order=order.pk)["charge"], "0.01")
        self.assertEqual(self.balance(), Decimal("99.99"))

    def test_the_incoming_v1_api_charges_the_same_way(self):
        payload = {"service_id": self.service.pk, "external_order_id": "ref-1", "target_link": "https://example.com/p", "quantity": 25}
        with PLENTY:
            response = self.api.post("/api/v1/orders/incoming/", payload, format="json", HTTP_X_API_KEY=self.alpha_key)
            retry = self.api.post("/api/v1/orders/incoming/", payload, format="json", HTTP_X_API_KEY=self.alpha_key)
        self.assertEqual((response.status_code, response.data["charge_amount"]), (200, "5.00"))
        self.assertEqual(retry.data["order"], response.data["order"])  # a retry is never charged again
        self.assertEqual(self.balance(), Decimal("95.00"))
        self.assertEqual(self.rows(Type.ORDER_CHARGE).count(), 1)

    def test_manual_admin_orders_have_no_wallet_and_no_charge(self):
        with PLENTY:
            order, _ = create_admin_order(service_id=self.service.pk, target_link="https://example.com/m", quantity=50,
                                          idempotency_key=uuid.uuid4())
        self.assertEqual((order.admin_wallet_id, order.charge_amount), (None, None))
        cancel_task(task_id=order.tasks.get().pk, reviewer=self.superuser, reason="broken_link")
        self.assertFalse(AdminWalletTransaction.objects.filter(order=order).exists())
        self.assertEqual(self.balance(), Decimal("100.00"))


class InsufficientFundsTests(AdminWalletTestCase):
    def test_no_balance_no_order(self):
        poor, poor_key = make_admin_wallet("Poor", balance="9.99")
        self.assertEqual(self.add(key=poor_key, quantity=50), NOT_ENOUGH)
        empty, empty_key = make_admin_wallet("Empty", balance="0")
        self.assertEqual(self.add(key=empty_key, quantity=10), NOT_ENOUGH)
        self.assertFalse(Order.objects.exists())
        self.assertEqual(self.balance(poor), Decimal("9.99"))
        self.assertFalse(AdminWalletTransaction.objects.exclude(transaction_type=Type.ADD_FUNDS).exists())
        with PLENTY:
            response = self.api.post("/api/v1/orders/incoming/", {
                "service_id": self.service.pk, "external_order_id": "x", "target_link": "https://example.com/p", "quantity": 50,
            }, format="json", HTTP_X_API_KEY=poor_key)
        self.assertEqual((response.status_code, response.data["detail"]), (400, ["Not enough funds on balance"]))

    def test_an_order_that_rounds_to_nothing_is_refused(self):
        tiny = make_service(name="Tiny", price_per_1000=Decimal("0.4000"), minimum_order_quantity=1)
        self.assertEqual(self.add(quantity=10, service=tiny), {"error": "The order amount is below ₹0.01. Increase the quantity."})
        self.assertFalse(Order.objects.exists())


class ParallelOrderTests(TransactionTestCase):
    """Concurrent orders on one wallet are charged one after another and never overdraw it."""

    def setUp(self):
        self.service = make_service(name="Race")
        self.racer, _key = make_admin_wallet("Racer", balance="25.00")

    def place(self, barrier):
        close_old_connections()
        try:
            wallet = AdminWallet.objects.get(pk=self.racer.pk)
            barrier.wait(timeout=5)
            create_provider_order(admin_wallet=wallet, service_id=self.service.pk, target_link=unique_link(), quantity=50)
            return True
        except ValidationError:
            return False
        finally:
            close_old_connections()

    def test_parallel_orders_cannot_overdraw(self):
        barrier = Barrier(5)
        with PLENTY, ThreadPoolExecutor(max_workers=5) as pool:
            outcomes = list(pool.map(lambda _index: self.place(barrier), range(5)))
        self.assertEqual(sum(outcomes), 2)  # ₹25.00 pays for two ₹10.00 orders
        self.assertEqual(AdminWallet.objects.get(pk=self.racer.pk).balance, Decimal("5.00"))
        self.assertEqual(Order.objects.count(), 2)
        charges = AdminWalletTransaction.objects.filter(transaction_type=Type.ORDER_CHARGE)
        self.assertEqual(sorted(charges.values_list("balance_after", flat=True)), [Decimal("5.00"), Decimal("15.00")])


class RefundTests(AdminWalletTestCase):
    def test_a_cancelled_order_gets_its_full_charge_back(self):
        order = self.order(quantity=20)  # ₹4.00
        self.assertEqual(self.balance(), Decimal("96.00"))
        cancel_task(task_id=order.tasks.get().pk, reviewer=self.superuser, reason="broken_link")
        order.refresh_from_db()
        self.assertEqual(order.fulfillment_status, Order.FulfillmentStatus.CANCELLED)
        refund = self.rows(Type.REFUND).get()
        self.assertEqual((refund.direction, refund.amount, refund.balance_after, refund.refund_order_id),
                         ("credit", Decimal("4.00"), Decimal("100.00"), order.pk))
        self.assertEqual(self.balance(), Decimal("100.00"))

    def test_partial_refund_is_charge_times_remains_over_quantity_rounded_down(self):
        thirds = make_service(name="Thirds", price_per_1000=Decimal("333.3333"), minimum_order_quantity=1)
        order = self.order(quantity=3, service=thirds)  # 0.9999999 -> ₹1.00
        self.assertEqual(order.charge_amount, Decimal("1.00"))
        task = self.deliver(order, 1)
        cancel_task(task_id=task.pk, reviewer=self.superuser, reason="target_removed")
        order.refresh_from_db()
        self.assertEqual(order.fulfillment_status, Order.FulfillmentStatus.PARTIAL)
        # 1.00 x 2 / 3 = 0.666…: rounded down to 0.66, never up to 0.67.
        self.assertEqual(self.rows(Type.REFUND).get().amount, Decimal("0.66"))
        self.assertEqual(self.balance(), Decimal("99.66"))
        self.assertEqual(self.v2(self.alpha_key, action="status", order=order.pk)["remains"], "2")

    def test_refund_math(self):
        order = Order(charge_amount=Decimal("10.00"), quantity=50, fulfillment_status=Order.FulfillmentStatus.PARTIAL)
        self.assertEqual(refund_due(order, 13), Decimal("7.40"))
        self.assertEqual(refund_due(order, 0), Decimal("10.00"))
        order.charge_amount, order.quantity = Decimal("0.01"), 3
        self.assertEqual(refund_due(order, 1), Decimal("0.00"))  # 0.0066 rounds down to nothing
        order.fulfillment_status = Order.FulfillmentStatus.CANCELLED
        self.assertEqual(refund_due(order, 0), Decimal("0.01"))
        order.fulfillment_status = Order.FulfillmentStatus.COMPLETED
        self.assertEqual(refund_due(order, 3), Decimal("0"))

    def test_a_partial_refund_that_rounds_to_nothing_writes_no_row(self):
        cheap = make_service(name="Cheap", price_per_1000=Decimal("0.5000"), minimum_order_quantity=1)
        order = self.order(quantity=10, service=cheap)  # ₹0.01
        task = self.deliver(order, 1)
        cancel_task(task_id=task.pk, reviewer=self.superuser, reason="target_removed")
        self.assertFalse(self.rows(Type.REFUND).exists())  # 0.01 x 9 / 10 = 0.009 -> 0.00
        self.assertEqual(self.mismatches(), [])

    def test_refunds_happen_exactly_once(self):
        order = self.order(quantity=10)  # ₹2.00
        task = order.tasks.get()
        cancel_task(task_id=task.pk, reviewer=self.superuser, reason="broken_link")
        first = self.rows(Type.REFUND).get()
        self.assertEqual(refund_api_order(order.pk).pk, first.pk)
        cancel_task(task_id=task.pk, reviewer=self.superuser, reason="broken_link")
        Order.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(hours=200))
        self.assertEqual(expire_old_orders(), 0)
        self.assertEqual(self.rows(Type.REFUND).count(), 1)
        self.assertEqual(self.balance(), Decimal("100.00"))
        # The database refuses a second refund row for the order as well.
        with self.assertRaises(IntegrityError), transaction.atomic():
            AdminWalletTransaction.objects.create(wallet=self.alpha, transaction_type=Type.REFUND, direction="credit",
                                                  amount=Decimal("2.00"), balance_after=Decimal("102.00"), order=order, refund_order=order)

    def test_order_expiry_refunds_through_the_same_function(self):
        untouched = self.order(quantity=10)
        worked = self.order(quantity=10)
        self.deliver(worked, 4)
        Order.objects.filter(pk__in=[untouched.pk, worked.pk]).update(created_at=timezone.now() - timedelta(hours=100))
        self.assertEqual(expire_old_orders(), 2)
        self.assertEqual(AdminWalletTransaction.objects.get(refund_order=untouched).amount, Decimal("2.00"))
        self.assertEqual(AdminWalletTransaction.objects.get(refund_order=worked).amount, Decimal("1.20"))  # 2.00 x 6 / 10
        self.assertEqual(self.balance(), Decimal("100.00") - Decimal("4.00") + Decimal("3.20"))
        self.assertEqual(self.mismatches(), [])

    def test_completed_orders_are_not_refunded(self):
        order = self.order(quantity=10)
        self.deliver(order, 10)
        order.refresh_from_db()
        self.assertEqual(order.fulfillment_status, Order.FulfillmentStatus.COMPLETED)
        self.assertIsNone(refund_api_order(order.pk))
        self.assertFalse(self.rows(Type.REFUND).exists())

    def test_deleting_an_unclaimed_task_refunds_and_charged_orders_are_never_deleted(self):
        order = self.order(quantity=10)
        response = self.root.post("/api/v1/admin/tasks/bulk-delete/", {"ids": [order.tasks.get().pk]}, format="json")
        self.assertEqual(response.data["deleted"], 1)
        self.assertEqual(AdminWalletTransaction.objects.get(refund_order=order).amount, Decimal("2.00"))
        response = self.root.post("/api/v1/admin/orders/bulk-delete/", {"ids": [order.pk]}, format="json")
        self.assertEqual(response.data["skipped"], 1)
        self.assertTrue(Order.objects.filter(pk=order.pk).exists())


class WalletScopeTests(AdminWalletTestCase):
    def test_status_and_multi_status_see_only_orders_charged_to_that_wallet(self):
        beta, beta_key = make_admin_wallet("Beta", balance="50.00")
        mine = self.order(quantity=10)
        theirs = self.order(wallet=beta, quantity=10)
        with PLENTY:
            manual, _ = create_admin_order(service_id=self.service.pk, target_link="https://example.com/m", quantity=10,
                                           idempotency_key=uuid.uuid4())
        self.assertEqual(self.v2(beta_key, action="status", order=mine.pk), {"error": "Incorrect order ID"})
        self.assertEqual(self.v2(self.alpha_key, action="status", order=manual.pk), {"error": "Incorrect order ID"})
        self.assertEqual(self.v2(self.alpha_key, action="status", order=mine.pk)["status"], "Pending")
        body = self.v2(beta_key, action="status", orders=f"{mine.pk},{theirs.pk},{manual.pk}")
        self.assertEqual(body[str(mine.pk)], {"error": "Incorrect order ID"})
        self.assertEqual(body[str(manual.pk)], {"error": "Incorrect order ID"})
        self.assertEqual(body[str(theirs.pk)]["charge"], "2.00")
        self.assertEqual(self.v2(beta_key, action="balance")["balance"], "48.00")

    def test_external_order_ids_belong_to_each_wallet(self):
        beta, beta_key = make_admin_wallet("Beta", balance="50.00")
        payload = {"service_id": self.service.pk, "external_order_id": "same-ref", "target_link": "https://example.com/p", "quantity": 10}
        with PLENTY:
            first = self.api.post("/api/v1/orders/incoming/", payload, format="json", HTTP_X_API_KEY=self.alpha_key)
            # Another link: the same link and service would be refused while Alpha's order is open.
            second = self.api.post("/api/v1/orders/incoming/", {**payload, "target_link": "https://example.com/q"}, format="json", HTTP_X_API_KEY=beta_key)
        self.assertNotEqual(first.data["order"], second.data["order"])  # Beta never receives Alpha's order
        self.assertEqual(Order.objects.get(pk=second.data["order"]).admin_wallet_id, beta.pk)
        self.assertEqual((self.balance(), self.balance(beta)), (Decimal("98.00"), Decimal("48.00")))


class KeyTests(AdminWalletTestCase):
    def test_an_inactive_or_non_staff_owner_is_refused_like_an_unknown_key(self):
        for change in ({"is_active": False}, {"is_staff": False}):
            User.objects.filter(pk=self.owner.pk).update(**change)
            for action in ("balance", "services", "status"):
                self.assertEqual(self.v2(self.alpha_key, action=action), INVALID, change)
            self.assertEqual(self.add(), INVALID)
            self.assertEqual(self.api.get("/api/v1/services/incoming/", HTTP_X_API_KEY=self.alpha_key).status_code, 403)
            User.objects.filter(pk=self.owner.pk).update(is_active=True, is_staff=True)
        self.assertEqual(self.v2(self.alpha_key, action="balance")["balance"], "100.00")

    def test_losing_staff_while_an_order_is_on_its_way_refuses_it(self):
        stale = AdminWallet.objects.get(pk=self.alpha.pk)
        User.objects.filter(pk=self.owner.pk).update(is_staff=False)
        with self.assertRaisesMessage(ValidationError, "Invalid API key"):
            self.order(wallet=stale)
        self.assertFalse(Order.objects.exists())

    def test_regenerating_shows_the_key_once_and_the_old_key_stops_at_once(self):
        response = self.me.post(f"{MY}regenerate-key/")
        self.assertEqual(response.status_code, 201)
        self.assertIn("no-store", response["Cache-Control"])
        new_key = response.data["api_key"]
        self.assertGreaterEqual(len(new_key), 40)
        self.assertEqual(response.data["wallet"]["api_key"]["prefix"], new_key[:8])
        self.assertEqual(self.v2(self.alpha_key, action="balance"), INVALID)
        self.assertEqual(self.v2(new_key, action="balance")["balance"], "100.00")
        self.assertEqual(AdminWallet.objects.get(pk=self.alpha.pk).api_key_hash, hash_api_key(new_key))
        detail = self.me.get(MY)
        self.assertNotIn(new_key, str(detail.data))
        self.assertNotIn(self.alpha.api_key_hash, str(detail.data))
        self.assertIsNone(authenticate_admin_wallet("not-a-key"))
        self.assertIsNone(authenticate_admin_wallet("x" * 300))


class MyWalletTests(AdminWalletTestCase):
    def test_each_admin_sees_only_their_own_wallet(self):
        beta, _key = make_admin_wallet("Beta", balance="7.00")
        data = self.me.get(MY).data
        self.assertEqual((data["id"], data["balance"], data["admin"]["id"], data["can_view_all_wallets"]), (self.alpha.pk, "100.00", self.owner.pk, False))
        other = APIClient()
        other.force_authenticate(beta.admin)
        self.assertEqual(other.get(MY).data["balance"], "7.00")
        self.assertEqual(self.root.get(MY).data["can_view_all_wallets"], True)

    def test_add_funds_needs_a_reference_and_is_recorded_once(self):
        url = f"{MY}add-funds/"
        missing = self.me.post(url, {"amount": "500.00", "reference": " "}, format="json")
        self.assertEqual((missing.status_code, list(missing.data)), (400, ["reference"]))
        for bad in ("0", "-5", "1.234", "abc"):
            self.assertEqual(self.me.post(url, {"amount": bad, "reference": "UTR1"}, format="json").status_code, 400, bad)
        key = str(uuid.uuid4())
        first = self.me.post(url, {"amount": "500.00", "reference": "UTR123456789", "note": "Oct top-up", "request_key": key}, format="json")
        again = self.me.post(url, {"amount": "500.00", "reference": "UTR123456789", "note": "Oct top-up", "request_key": key}, format="json")
        self.assertEqual((first.status_code, again.status_code, again.data["created"]), (201, 200, False))
        self.assertEqual(first.data["wallet"]["balance"], "600.00")
        self.assertEqual(first.data["transaction"]["created_by"], self.owner.email)
        self.assertEqual(self.rows(Type.ADD_FUNDS).filter(reference="UTR123456789").count(), 1)

    def test_history_is_paginated_by_20_and_filters_by_type_and_date(self):
        order = self.order(quantity=10)
        cancel_task(task_id=order.tasks.get().pk, reviewer=self.superuser, reason="broken_link")
        ledger = self.me.get(f"{MY}transactions/").data
        self.assertEqual([row["transaction_type"] for row in ledger["results"]], ["refund", "order_charge", "add_funds"])
        self.assertEqual(ledger["results"][0]["order_id"], order.pk)
        refunds = self.me.get(f"{MY}transactions/", {"transaction_type": "refund"}).data
        self.assertEqual((refunds["count"], refunds["results"][0]["amount"]), (1, "2.00"))
        today = timezone.localdate().isoformat()
        self.assertEqual(self.me.get(f"{MY}transactions/", {"date_from": today, "date_to": today}).data["count"], 3)
        tomorrow = (timezone.localdate() + timedelta(days=1)).isoformat()
        self.assertEqual(self.me.get(f"{MY}transactions/", {"date_from": tomorrow}).data["count"], 0)
        self.assertEqual(self.me.get(f"{MY}transactions/", {"transaction_type": "bogus"}).status_code, 400)
        for index in range(25):
            self.me.post(f"{MY}add-funds/", {"amount": "1.00", "reference": f"UTR-{index}"}, format="json")
        page = self.me.get(f"{MY}transactions/").data
        self.assertEqual((page["count"], len(page["results"])), (28, 20))
        self.assertEqual(len(self.me.get(f"{MY}transactions/", {"page": 2}).data["results"]), 8)

    def test_this_months_summary(self):
        self.order(quantity=20)  # ₹4.00
        refunded = self.order(quantity=10)  # ₹2.00, refunded
        cancel_task(task_id=refunded.tasks.get().pk, reviewer=self.superuser, reason="broken_link")
        self.me.post(f"{MY}add-funds/", {"amount": "10.00", "reference": "UTR-M"}, format="json")
        data = self.me.get(MY).data
        self.assertEqual(data["this_month"], {"added": "110.00", "spent": "6.00", "refunded": "2.00"})
        self.assertEqual((data["balance"], data["low_balance"]), ("106.00", True))  # below the ₹500.00 default threshold
        patched = self.me.patch(MY, {"low_balance_threshold": "50.00"}, format="json")
        self.assertEqual((patched.status_code, patched.data["low_balance"]), (200, False))
        self.assertEqual(self.me.patch(MY, {"balance": "1"}, format="json").status_code, 400)

    def test_only_admins_reach_my_wallet_and_only_superusers_reach_admin_wallets(self):
        worker = APIClient()
        worker.force_authenticate(make_user(70))
        for response in (self.api.get(MY), worker.get(MY), worker.post(f"{MY}add-funds/", {"amount": "5", "reference": "R"}, format="json"),
                         worker.get(ALL), self.me.get(ALL), self.me.get(f"{ALL}{self.alpha.pk}/"),
                         self.me.get(f"{ALL}{self.alpha.pk}/transactions/"),
                         self.me.post(f"{ALL}{self.alpha.pk}/adjustments/", {"amount": "5", "note": "x"}, format="json")):
            self.assertIn(response.status_code, (401, 403))


class AdminWalletsOverviewTests(AdminWalletTestCase):
    def test_superusers_see_every_admins_wallet_and_history(self):
        beta, _key = make_admin_wallet("Beta", balance="7.00")
        no_wallet = make_user(84, is_staff=True)
        AdminWallet.objects.filter(admin=no_wallet).delete()  # e.g. made staff with a queryset update
        rows = {row["admin"]["id"]: row for row in self.root.get(ALL).data["results"]}
        self.assertEqual(set(rows), {self.owner.pk, beta.admin_id, self.superuser.pk, no_wallet.pk})
        self.assertEqual((rows[beta.admin_id]["balance"], rows[no_wallet.pk]["balance"]), ("7.00", "0.00"))
        self.assertNotIn("api_key_hash", str(rows))
        history = self.root.get(f"{ALL}{beta.pk}/transactions/").data
        self.assertEqual([row["amount"] for row in history["results"]], ["7.00"])
        self.assertEqual(self.root.get(f"{ALL}{beta.pk}/").data["balance"], "7.00")

    def test_adjustments_need_a_note_never_go_below_zero_and_are_audit_logged(self):
        url = f"{ALL}{self.alpha.pk}/adjustments/"
        self.assertEqual(self.root.post(url, {"amount": "-5.00"}, format="json").status_code, 400)
        below = self.root.post(url, {"amount": "-100.01", "note": "Correction"}, format="json")
        self.assertEqual(below.status_code, 400)
        self.assertIn("below ₹0.00", str(below.data))
        down = self.root.post(url, {"amount": "-100.00", "note": "Wrong deposit"}, format="json")
        up = self.root.post(url, {"amount": "12.50", "note": "Goodwill"}, format="json")
        self.assertEqual((down.status_code, up.status_code), (201, 201))
        self.assertEqual(self.balance(), Decimal("12.50"))
        self.assertEqual(sorted(self.rows(Type.ADJUSTMENT).values_list("direction", flat=True)), ["credit", "debit"])
        log = list(UserChangeLog.objects.filter(user=self.owner, field="admin_wallet_adjustment").order_by("id")
                   .values_list("old_value", "new_value", "changed_by_id"))
        self.assertEqual(log, [("100.00", "0.00 (-100.00: Wrong deposit)", self.superuser.pk),
                               ("0.00", "12.50 (+12.50: Goodwill)", self.superuser.pk)])
        with self.assertRaises(ValidationError):
            record_adjustment(wallet_id=self.alpha.pk, amount="-12.51", note="x", by=self.superuser)

    def test_analytics_orders_can_be_filtered_by_admin_wallet(self):
        beta, _key = make_admin_wallet("Beta", balance="50.00")
        mine, theirs = self.order(quantity=10), self.order(wallet=beta, quantity=10)
        with PLENTY:
            manual, _ = create_admin_order(service_id=self.service.pk, target_link="https://example.com/m", quantity=10, idempotency_key=uuid.uuid4())
        ids = lambda params: [row["id"] for row in self.root.get("/api/v1/admin/analytics/orders/", params).data["results"]]  # noqa: E731
        self.assertEqual(ids({"admin_wallet": self.alpha.pk}), [mine.pk])
        self.assertEqual(ids({"admin_wallet": beta.pk}), [theirs.pk])
        self.assertEqual(ids({"admin_wallet": "manual"}), [manual.pk])
        self.assertEqual(len(ids({})), 3)
        self.assertEqual(self.root.get("/api/v1/admin/analytics/orders/", {"admin_wallet": "x"}).status_code, 400)


@CONFIGURED
class AdminWalletAlertTests(AdminWalletTestCase):
    def setUp(self):
        super().setUp()
        self.urlopen = patch("common.notifications.urllib.request.urlopen", return_value=telegram_ok()).start()
        self.addCleanup(patch.stopall)
        self.low, self.low_key = make_admin_wallet("Lowbal Admin", balance="60.00", low_balance_threshold="50.00")
        self.low_admin = APIClient()
        self.low_admin.force_authenticate(self.low.admin)

    def texts(self, words):
        return [text for text in sent_texts(self.urlopen) if words in text]

    def test_low_balance_is_announced_once_per_crossing(self):
        for _ in range(3):  # 50.00 (not below), 40.00 (below: announced), 30.00 (still below)
            with self.captureOnCommitCallbacks(execute=True):
                self.add(key=self.low_key)
        self.assertEqual(self.texts("low-balance threshold"), [
            f"[TaskZippy] Admin wallet #{self.low.pk} (admin #{self.low.admin_id}): balance ₹40.00 is below its low-balance "
            "threshold of ₹50.00. Add funds so its API orders keep being accepted."])
        with self.captureOnCommitCallbacks(execute=True):
            self.low_admin.post(f"{MY}add-funds/", {"amount": "30.00", "reference": "UTR-SECRET-99"}, format="json")
        for _ in range(2):  # 50.00, then 40.00: a new crossing
            with self.captureOnCommitCallbacks(execute=True):
                self.add(key=self.low_key)
        self.assertEqual(len(self.texts("low-balance threshold")), 2)
        self.assertEqual(self.texts("Funds of"), [
            f"[TaskZippy] Funds of ₹30.00 added to admin wallet #{self.low.pk} (admin #{self.low.admin_id}) "
            f"by admin #{self.low.admin_id}. New balance ₹60.00."])
        # IDs and amounts only: never the payment reference, the admin's name or email.
        self.assertFalse(any("UTR-SECRET-99" in text or "Lowbal" in text or "@" in text for text in sent_texts(self.urlopen)))

    def test_insufficient_funds_alerts_are_batched_per_admin(self):
        poor, poor_key = make_admin_wallet("Poor", balance="1.00")
        for _ in range(3):
            with self.captureOnCommitCallbacks(execute=True):
                self.assertEqual(self.add(key=poor_key), NOT_ENOUGH)
        self.assertEqual(self.texts("refused for insufficient funds"), [
            f"[TaskZippy] Admin wallet #{poor.pk} (admin #{poor.admin_id}): 1 order(s) refused for insufficient funds. Balance ₹1.00."])
        self.assertEqual(AdminWallet.objects.get(pk=poor.pk).refused_orders_unreported, 2)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(flush_refused_order_alerts(), 0)  # still inside the 15 minutes
        AdminWallet.objects.filter(pk=poor.pk).update(refused_alert_at=timezone.now() - timedelta(minutes=16))
        with self.captureOnCommitCallbacks(execute=True):
            call_command("expire_claims", stdout=StringIO())  # the cron sends what is due
        batched = self.texts("refused for insufficient funds")
        self.assertEqual(len(batched), 2)
        self.assertIn(f"Admin wallet #{poor.pk} (admin #{poor.admin_id}): 2 order(s) refused for insufficient funds since ", batched[1])
        self.assertEqual(AdminWallet.objects.get(pk=poor.pk).refused_orders_unreported, 0)


class AdminWalletReconciliationTests(AdminWalletTestCase):
    def setUp(self):
        super().setUp()
        self.charged = self.order(quantity=20)  # ₹4.00
        self.cancelled = self.order(quantity=10)  # ₹2.00, refunded
        cancel_task(task_id=self.cancelled.tasks.get().pk, reviewer=self.superuser, reason="broken_link")
        record_adjustment(wallet_id=self.alpha.pk, amount="-1.50", note="Correction", by=self.superuser)

    def test_a_consistent_ledger_is_clean(self):
        self.assertEqual(self.mismatches(), [])
        self.assertEqual(self.balance(), Decimal("94.50"))

    def test_an_altered_balance_is_detected(self):
        AdminWallet.objects.filter(pk=self.alpha.pk).update(balance=Decimal("500.00"))
        self.assertEqual(self.mismatches(), ["admin_wallet_balance"])
        self.assertEqual(self.balance(), Decimal("500.00"))  # reported, never corrected

    def test_an_altered_charge_or_a_missing_refund_is_detected(self):
        AdminWalletTransaction.objects.filter(charge_order=self.charged).update(amount=Decimal("0.40"))
        self.assertEqual(self.mismatches(), ["admin_wallet_balance", "api_order_charge"])
        AdminWalletTransaction.objects.filter(charge_order=self.charged).update(amount=Decimal("4.00"))
        AdminWalletTransaction.objects.filter(refund_order=self.cancelled).delete()
        self.assertEqual(self.mismatches(), ["admin_wallet_balance", "api_order_refund"])

    def test_an_altered_order_charge_or_an_uncharged_order_is_detected(self):
        Order.objects.filter(pk=self.charged.pk).update(charge_amount=Decimal("0.04"))
        self.assertEqual(self.mismatches(), ["api_charge_amount", "api_order_charge"])
        Order.objects.filter(pk=self.charged.pk).update(charge_amount=Decimal("4.00"))
        AdminWalletTransaction.objects.filter(charge_order=self.charged).delete()
        Order.objects.filter(pk=self.charged.pk).update(charge_amount=None)
        self.assertEqual(self.mismatches(), ["admin_wallet_balance", "api_order_without_charge"])

    def test_a_partial_order_without_its_refund_is_detected(self):
        self.deliver(self.charged, 2)
        Order.objects.filter(pk=self.charged.pk).update(fulfillment_status=Order.FulfillmentStatus.PARTIAL)
        self.assertEqual(self.mismatches(), ["api_order_refund"])  # Partial but never refunded

    def test_staff_without_a_wallet_and_ex_staff_wallets_are_notices(self):
        lost = make_user(85, is_staff=True)
        AdminWallet.objects.filter(admin=lost).delete()
        User.objects.filter(pk=self.owner.pk).update(is_staff=False)
        with self.captureOnCommitCallbacks(execute=True):
            run = reconcile_wallets()
        checks = {item["check"]: item for item in run.details["notices"]}
        self.assertEqual(checks["staff_without_admin_wallet"]["admin_ids"], [lost.pk])
        self.assertEqual(checks["admin_wallet_owner_not_staff"]["admin_wallet_id"], self.alpha.pk)
        self.assertEqual(run.mismatches_count, 0)

    def test_the_database_keeps_one_charge_per_order_and_well_formed_rows(self):
        bad_rows = [
            dict(transaction_type=Type.ORDER_CHARGE, direction="debit", order=self.charged, charge_order=self.charged),
            dict(transaction_type=Type.REFUND, direction="credit", order=None, refund_order=None),
            dict(transaction_type=Type.ADD_FUNDS, direction="credit", reference=""),
            dict(transaction_type=Type.ADD_FUNDS, direction="debit", reference="UTR"),
            dict(transaction_type=Type.ADJUSTMENT, direction="credit", order=self.charged, charge_order=self.charged),
            dict(transaction_type=Type.ADJUSTMENT, direction="credit", order=self.charged),
        ]
        for fields in bad_rows:
            with self.subTest(fields=fields), self.assertRaises(IntegrityError), transaction.atomic():
                AdminWalletTransaction.objects.create(wallet=self.alpha, amount=Decimal("1.00"), balance_after=Decimal("1.00"), **fields)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AdminWallet.objects.filter(pk=self.alpha.pk).update(balance=Decimal("-0.01"))


class MigrationRunner(TransactionTestCase):
    """Runs the orders migrations from `before` to the latest, with data added in between."""

    def migrate_with(self, before, populate):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        output = StringIO()
        try:
            with redirect_stdout(StringIO()):
                executor.migrate(before)
            # accounts stays fully migrated, so its models must be in the state the data is written with.
            accounts = [node for node in latest if node[0] == "accounts"]
            result = populate(executor.loader.project_state(before + accounts).apps)
            executor = MigrationExecutor(connection)
            with redirect_stdout(output):
                executor.migrate(latest)
        finally:
            executor = MigrationExecutor(connection)
            if executor.migration_plan(latest):
                with redirect_stdout(StringIO()):
                    executor.migrate(latest)
        return result, output.getvalue()


def old_user(apps, email, code, **extra):
    fields = dict(email=email, password="!", full_name=email, platform_username=email.split("@")[0], whatsapp_number="9",
                  gender="female", referral_code=code, approval_status="approved", is_active=True)
    fields.update(extra)
    return apps.get_model("accounts", "User").objects.create(**fields)


def old_order(apps, service, **extra):
    fields = dict(service_id=service.pk, source="api", target_link="https://example.com/old", quantity=100,
                  price_per_1000_snapshot=Decimal("200.0000"), business_share_percentage_snapshot=Decimal("75.00"),
                  user_reward_percentage_snapshot=Decimal("25.00"), capacity_percentage_snapshot=Decimal("100.00"),
                  total_order_value=Decimal("20.00000000"))
    fields.update(extra)
    return apps.get_model("orders", "Order").objects.create(**fields)


@override_settings(PROVIDER_API_REQUIRE_HTTPS=False)
class Phase6KeyMigrationTests(MigrationRunner):
    """The single Phase 6 key (orders.0011 -> "Default" client) ends on the main superuser's wallet and keeps working."""

    def test_the_old_key_keeps_working_on_the_main_superusers_wallet(self):
        service = make_service(name="Legacy")
        old_key = "legacy-perfect-panel-key-0123456789abcdefXYZ"

        def populate(apps):
            boss = old_user(apps, "boss@example.com", "BOSS2345", is_staff=True, is_superuser=True)
            old_user(apps, "helper@example.com", "HELP2345", is_staff=True)
            apps.get_model("orders", "IncomingApiKey").objects.create(
                pk=1, key_hash=hash_api_key(old_key), key_hint=old_key[-4:], rotated_at=timezone.now())
            legacy = old_order(apps, service, external_order_id="legacy-1", quantity=10, total_order_value=Decimal("2.00000000"))
            return boss.pk, legacy.pk

        (boss_id, legacy_id), output = self.migrate_with([("orders", "0010_api_clients")], populate)
        wallet = AdminWallet.objects.get(admin_id=boss_id)
        self.assertEqual((wallet.balance, wallet.api_key_prefix), (Decimal("0.00"), "…" + old_key[-4:]))
        self.assertEqual(authenticate_admin_wallet(old_key).pk, wallet.pk)
        self.assertEqual(AdminWallet.objects.count(), 2)  # the helper (staff) has one too, without a key
        self.assertEqual((Order.objects.get(pk=legacy_id).admin_wallet_id, Order.objects.get(pk=legacy_id).charge_amount), (wallet.pk, None))
        self.assertIn(f"admin #{boss_id}'s wallet #{wallet.pk}: the existing Phase 6 reseller key (prefix …{old_key[-4:]}) keeps working",
                      output)

        api = APIClient()
        reply = lambda **data: api.post(URL, urlencode({"key": old_key, **data}), content_type="application/x-www-form-urlencoded").json()  # noqa: E731
        self.assertEqual(reply(action="balance"), {"balance": "0.00", "currency": "INR"})
        status = reply(action="status", order=legacy_id)
        self.assertEqual((status["status"], status["charge"]), ("Pending", "2.00"))
        with PLENTY:
            self.assertEqual(reply(action="add", service=service.pk, link="https://example.com/p", quantity=10), NOT_ENOUGH)
        # The order placed before balances is a notice, never a mismatch, and is never refunded.
        run = reconcile_wallets()
        self.assertEqual(run.mismatches_count, 0)
        self.assertIn("api_orders_before_balances", [item["check"] for item in run.details["notices"]])
        Order.objects.filter(pk=legacy_id).update(fulfillment_status=Order.FulfillmentStatus.CANCELLED)
        self.assertIsNone(refund_api_order(legacy_id))


class ApiClientConversionMigrationTests(MigrationRunner):
    """orders.0014: balances, ledgers, orders and keys of several API clients move to their admins' wallets."""

    def test_clients_merge_into_their_admins_wallets_and_still_reconcile(self):
        service = make_service(name="Converted")
        keys = {"default": "default-key-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "second": "second-key-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
                "ana": "ana-panel-key-CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC"}

        def populate(apps):
            Client = apps.get_model("orders", "ApiClient")
            Row = apps.get_model("orders", "ApiClientTransaction")
            boss = old_user(apps, "boss@example.com", "BOSS2345", is_staff=True, is_superuser=True)
            ana = old_user(apps, "ana@example.com", "ANA23456", is_staff=True)
            default = Client.objects.create(name="Default", key_prefix="…AAAA", key_hash=hash_api_key(keys["default"]), balance=Decimal("80.00"))
            second = Client.objects.create(name="Second", key_prefix="second-k", key_hash=hash_api_key(keys["second"]),
                                           balance=Decimal("30.00"), created_by=boss)
            panel = Client.objects.create(name="Ana panel", key_prefix="ana-pane", key_hash=hash_api_key(keys["ana"]),
                                          balance=Decimal("50.00"), created_by=ana)
            charged = old_order(apps, service, api_client=default, external_order_id="same", charge_amount=Decimal("20.00"))
            old_order(apps, service, api_client=second, external_order_id="same", total_order_value=Decimal("2.00000000"), quantity=10)
            Row.objects.create(client=default, transaction_type="deposit", direction="credit", amount=Decimal("100.00"),
                               balance_after=Decimal("100.00"), reference="UTR-1")
            Row.objects.create(client=second, transaction_type="deposit", direction="credit", amount=Decimal("30.00"),
                               balance_after=Decimal("30.00"), reference="UTR-2")
            Row.objects.create(client=default, transaction_type="order_charge", direction="debit", amount=Decimal("20.00"),
                               balance_after=Decimal("80.00"), order=charged, charge_order=charged)
            Row.objects.create(client=panel, transaction_type="deposit", direction="credit", amount=Decimal("50.00"),
                               balance_after=Decimal("50.00"), reference="UTR-3")
            return boss.pk, ana.pk, charged.pk, second.pk

        (boss_id, ana_id, charged_id, second_id), output = self.migrate_with([("orders", "0013_admin_wallets")], populate)
        boss_wallet, ana_wallet = AdminWallet.objects.get(admin_id=boss_id), AdminWallet.objects.get(admin_id=ana_id)
        self.assertEqual((boss_wallet.balance, ana_wallet.balance), (Decimal("110.00"), Decimal("50.00")))
        ledger = list(boss_wallet.transactions.order_by("created_at", "id").values_list("transaction_type", "amount", "balance_after"))
        self.assertEqual(ledger, [("add_funds", Decimal("100.00"), Decimal("100.00")), ("add_funds", Decimal("30.00"), Decimal("130.00")),
                                  ("order_charge", Decimal("20.00"), Decimal("110.00"))])
        self.assertEqual(authenticate_admin_wallet(keys["default"]).pk, boss_wallet.pk)
        self.assertEqual(authenticate_admin_wallet(keys["ana"]).pk, ana_wallet.pk)
        self.assertIsNone(authenticate_admin_wallet(keys["second"]))  # one key per admin: Default's was kept
        self.assertIn("stops working", output)
        self.assertEqual(Order.objects.get(pk=charged_id).admin_wallet_id, boss_wallet.pk)
        # Both clients used "same"; in one wallet the second is renamed so (wallet, external ID) stays unique.
        self.assertEqual(sorted(Order.objects.filter(admin_wallet=boss_wallet).values_list("external_order_id", flat=True)),
                         ["same", f"same~client{second_id}"])
        self.assertIn(f"renamed to 'same~client{second_id}'", output)
        self.assertEqual(reconcile_wallets().mismatches_count, 0)
