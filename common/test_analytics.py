"""Admin Analytics figures, tabs and periods, checked against hand-computed values."""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from common import analytics
from orders.models import Order
from orders.testing import unique_link
from services_catalog.models import Service
from tasks.models import Task
from tasks.services import approve_submitted_task, cancel_task, submit_task_result
from tasks.test_claim_lifecycle import make_user, opened
from wallet.models import Wallet, WalletTransaction, WithdrawalRequest

TODAY = {"date_from": None, "date_to": None}


def make_order(service, quantity, **extra):
    order = Order.objects.create(
        service=service, source=Order.Source.MANUAL, target_link=unique_link("https://example.com/a"), quantity=quantity,
        price_per_1000_snapshot=service.price_per_1000, business_share_percentage_snapshot=service.business_share_percentage,
        user_reward_percentage_snapshot=service.user_reward_percentage, capacity_percentage_snapshot=Decimal("100.00"),
        fulfillment_cost_per_1000_snapshot=service.fulfillment_cost_per_1000,
        has_fulfillment_cost_snapshot=service.has_fulfillment_cost,
        total_order_value=service.price_per_1000 * quantity / 1000, **extra,
    )
    Task.objects.create(order=order, target_link=order.target_link, total_slots=quantity, remaining_slots=quantity,
                        price_per_1000_snapshot=service.price_per_1000, user_reward_percentage_snapshot=service.user_reward_percentage,
                        reward_amount=Decimal("0.02500000"))
    return order


class AnalyticsTests(TestCase):
    def setUp(self):
        self.admin = make_user(0, is_staff=True)
        self.api = APIClient()
        self.api.force_authenticate(self.admin)
        today = timezone.localdate().isoformat()
        self.params = {"date_from": today, "date_to": today}
        costly = Service.objects.create(name="Costly", price_per_1000=Decimal("100.0000"), has_fulfillment_cost=True,
                                        fulfillment_cost_per_1000=Decimal("10.0000"))
        free = Service.objects.create(name="No cost", price_per_1000=Decimal("100.0000"))
        self.workers = iter(make_user(index) for index in range(1, 40))

        self.completed = make_order(costly, 2)
        self.approve(self.completed, 2)
        self.partial = make_order(free, 4)
        self.approve(self.partial, 1)
        cancel_task(task_id=self.partial.tasks.get().pk, reviewer=self.admin, reason="target_removed")
        self.open = make_order(free, 3)
        self.old = make_order(free, 1)
        self.approve(self.old, 1)
        Order.objects.filter(pk=self.old.pk).update(created_at=timezone.now() - timedelta(days=40))

        worker = next(self.workers)
        wallet = Wallet.objects.create(user=worker, balance=Decimal("5.00000000"))
        WalletTransaction.objects.create(wallet=wallet, transaction_type="adjustment", direction="credit",
                                         amount=Decimal("5.00000000"), balance_after=Decimal("5.00000000"))
        now = timezone.now()
        WithdrawalRequest.objects.create(user=worker, amount=Decimal("1.00"), payout_method="upi", payout_details="w@upi",
                                         status="approved", reviewed_at=now)
        WithdrawalRequest.objects.create(user=worker, amount=Decimal("0.50"), payout_method="upi", payout_details="w@upi")
        WithdrawalRequest.objects.create(user=worker, amount=Decimal("2.00"), payout_method="upi", payout_details="w@upi",
                                         status="rejected", reviewed_at=now)
        self.adjusted_user = worker

    def approve(self, order, count):
        task = order.tasks.get()
        for _ in range(count):
            worker = next(self.workers)
            opened(task, worker)
            submit_task_result(task_id=task.pk, user=worker, result="work_done")
            approve_submitted_task(task_id=task.claims.get(user=worker).pk, reviewer=self.admin)

    def get(self, path="", **params):
        response = self.api.get(f"/api/v1/admin/analytics/{path}", {**self.params, **params})
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_summary_cards(self):
        data = self.get()
        # Completed: 2 x 100/1000 = 0.2; partial: 1 x 100/1000 = 0.1.
        self.assertEqual(data["recognized_revenue"], "0.30000000")
        self.assertEqual(data["closed_orders"], 2)
        # Rewards on those two Orders: 3 x 0.025; cost: 10/1000 x 2 delivered; the no-cost service adds 0.
        self.assertEqual(data["rewards_on_closed_orders"], "0.07500000")
        self.assertEqual(data["fulfillment_cost"], "0.02000000")
        self.assertEqual(data["our_share"], "0.20500000")
        self.assertTrue(data["our_share_available"])
        self.assertEqual((data["open_order_value"], data["open_orders"]), ("0.30000000", 1))
        # By credit date: includes the out-of-cohort Order's reward credited today.
        self.assertEqual(data["paid_to_users"], "0.10000000")
        self.assertEqual(data["adjustment_credits"], "5.00000000")
        self.assertEqual(data["upi_paid_out"], "1.00000000")
        self.assertEqual((data["pending_withdrawals"], data["pending_withdrawal_count"]), ("0.50000000", 1))
        self.assertEqual(data["owed_in_wallets"], str(sum(Wallet.objects.values_list("balance", flat=True), Decimal("0")).quantize(Decimal("0.00000001"))))
        self.assertEqual(data["period"]["timezone"], "Asia/Kolkata")

    def test_our_share_is_unavailable_when_a_required_cost_is_missing(self):
        unknown = Service.objects.create(name="Unknown cost", price_per_1000=Decimal("100.0000"), has_fulfillment_cost=True)
        order = make_order(unknown, 1)
        self.approve(order, 1)
        data = self.get()
        self.assertIsNone(data["our_share"])
        self.assertIsNone(data["fulfillment_cost"])
        self.assertFalse(data["our_share_available"])
        self.assertEqual(data["recognized_revenue"], "0.40000000")

    def test_orders_tab_rows_and_detail(self):
        rows = {row["id"]: row for row in self.get("orders/")["results"]}
        self.assertNotIn(self.old.pk, rows)
        partial = rows[self.partial.pk]
        self.assertEqual((partial["status"], partial["quantity"], partial["delivered"]), ("partial", 4, 1))
        self.assertEqual((partial["order_value"], partial["recognized_revenue"], partial["paid_to_users"], partial["our_share"]),
                         ("0.40000000", "0.10000000", "0.02500000", "0.07500000"))
        self.assertIsNone(rows[self.open.pk]["recognized_revenue"])
        detail = self.api.get(f"/api/v1/admin/analytics/orders/{self.completed.pk}/").data
        self.assertEqual(len(detail["approved_claims"]), 2)
        self.assertEqual({row["amount"] for row in detail["approved_claims"]}, {"0.02500000"})
        self.assertTrue(all(row["username"] for row in detail["approved_claims"]))

    def test_orders_tab_is_paginated_by_20(self):
        service = Service.objects.get(name="No cost")
        for _ in range(18):
            make_order(service, 1)
        first = self.get("orders/")
        self.assertEqual((first["count"], len(first["results"])), (21, 20))
        self.assertEqual(len(self.get("orders/", page=2)["results"]), 1)

    def test_users_tab(self):
        rows = {row["user_id"]: row for row in self.get("users/")["results"]}
        adjusted = rows[self.adjusted_user.pk]
        self.assertEqual((adjusted["withdrawn"], adjusted["earned"], adjusted["balance"]), ("1.00000000", "0.00000000", "5.00000000"))
        earners = [row for row in rows.values() if row["approved_tasks"]]
        self.assertEqual(len(earners), 4)
        self.assertTrue(all(row["earned"] == "0.02500000" for row in earners))
        self.assertNotIn(self.admin.pk, rows)

    def test_payouts_tab(self):
        rows = self.get("payouts/")["results"]
        self.assertEqual(len(rows), 3)
        approved = next(row for row in rows if row["status"] == "approved")
        self.assertEqual((approved["amount"], approved["upi_id"]), ("1.00000000", "w@upi"))
        self.assertIsNotNone(approved["approved_at"])
        self.assertIsNone(next(row for row in rows if row["status"] == "rejected")["approved_at"])

    def test_permissions_and_bad_periods(self):
        worker = APIClient()
        worker.force_authenticate(make_user(99))
        self.assertEqual(worker.get("/api/v1/admin/analytics/").status_code, 403)
        self.assertEqual(self.api.get("/api/v1/admin/analytics/", {"period": "forever"}).status_code, 400)
        self.assertEqual(self.api.get("/api/v1/admin/analytics/", {"period": "this_month", "date_from": "2026-01-01", "date_to": "2026-01-02"}).status_code, 400)
        self.assertEqual(self.api.get("/api/v1/admin/analytics/", {"date_from": "2026-02-02", "date_to": "2026-02-01"}).status_code, 400)


class PeriodTests(TestCase):
    def resolve(self, today, **params):
        with patch.object(analytics.timezone, "localdate", return_value=today):
            return analytics.resolve_period(params)

    def test_presets(self):
        self.assertEqual(self.resolve(date(2026, 10, 24)), (date(2026, 10, 1), date(2026, 10, 24)))
        self.assertEqual(self.resolve(date(2026, 10, 24), period="last_month"), (date(2026, 9, 1), date(2026, 9, 30)))
        self.assertEqual(self.resolve(date(2026, 1, 15), period="last_month"), (date(2025, 12, 1), date(2025, 12, 31)))
        self.assertEqual(self.resolve(date(2026, 2, 10), period="last_3_months"), (date(2025, 12, 1), date(2026, 2, 10)))
        self.assertEqual(self.resolve(date(2026, 10, 24), date_from="2026-03-01", date_to="2026-03-31"), (date(2026, 3, 1), date(2026, 3, 31)))

    def test_bounds_are_half_open_local_days(self):
        start, end = analytics.period_bounds(date(2026, 10, 1), date(2026, 10, 31))
        self.assertEqual((end - start), timedelta(days=31))
        self.assertEqual(timezone.localtime(start).hour, 0)
        self.assertEqual(str(timezone.localtime(start).tzinfo), "Asia/Kolkata")
