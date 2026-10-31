"""Admin Dashboard (Phase 5B): periods, previous periods, trend buckets, users block."""

from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from accounts.models import UserLevel
from accounts.strike_policy import active_levels, level_for_count
from common import analytics
from orders.models import Order
from services_catalog.models import Service
from tasks.models import Task, TaskClaim
from tasks.test_claim_lifecycle import make_user
from wallet.models import Wallet, WalletTransaction, WithdrawalRequest

from .test_analytics import make_order

URL = "/api/v1/admin/dashboard/stats/"


def utc(*parts):
    return datetime(*parts, tzinfo=dt_timezone.utc)


def period(key, today, **params):
    return analytics.dashboard_period({"period": key, **params}, today=today)


def spans(result):
    return (result["date_from"], result["date_to"], result["previous_from"], result["previous_to"])


class DashboardPeriodTests(TestCase):
    """Pure date arithmetic: no database rows needed."""

    def test_day_periods_compare_with_the_day_before(self):
        wednesday = date(2026, 10, 21)
        self.assertEqual(spans(period("today", wednesday)), (wednesday, wednesday, date(2026, 10, 20), date(2026, 10, 20)))
        self.assertEqual(spans(period("yesterday", wednesday)), (date(2026, 10, 20), date(2026, 10, 20), date(2026, 10, 19), date(2026, 10, 19)))

    def test_weeks_start_on_monday_and_this_week_compares_to_date(self):
        wednesday = date(2026, 10, 21)
        self.assertEqual(spans(period("this_week", wednesday)), (date(2026, 10, 19), wednesday, date(2026, 10, 12), date(2026, 10, 14)))
        self.assertEqual(spans(period("last_week", wednesday)), (date(2026, 10, 12), date(2026, 10, 18), date(2026, 10, 5), date(2026, 10, 11)))
        monday = date(2026, 10, 19)
        self.assertEqual(spans(period("this_week", monday)), (monday, monday, date(2026, 10, 12), date(2026, 10, 12)))
        sunday = date(2026, 10, 25)
        self.assertEqual(spans(period("this_week", sunday))[:2], (monday, sunday))

    def test_month_periods_clamp_to_the_shorter_previous_month(self):
        self.assertEqual(spans(period("this_month", date(2026, 3, 31))), (date(2026, 3, 1), date(2026, 3, 31), date(2026, 2, 1), date(2026, 2, 28)))
        self.assertEqual(spans(period("this_month", date(2026, 3, 15))), (date(2026, 3, 1), date(2026, 3, 15), date(2026, 2, 1), date(2026, 2, 15)))
        self.assertEqual(spans(period("this_month", date(2028, 3, 30)))[2:], (date(2028, 2, 1), date(2028, 2, 29)))

    def test_last_month_crosses_the_year(self):
        self.assertEqual(spans(period("last_month", date(2026, 1, 10))), (date(2025, 12, 1), date(2025, 12, 31), date(2025, 11, 1), date(2025, 11, 30)))
        self.assertEqual(spans(period("last_month", date(2026, 3, 1))), (date(2026, 2, 1), date(2026, 2, 28), date(2026, 1, 1), date(2026, 1, 31)))

    def test_custom_range_compares_with_the_same_length_before_it(self):
        custom = period("custom", date(2026, 10, 21), date_from="2026-03-10", date_to="2026-03-12")
        self.assertEqual(spans(custom), (date(2026, 3, 10), date(2026, 3, 12), date(2026, 3, 7), date(2026, 3, 9)))
        self.assertEqual(custom["label"], "Custom range")
        # Dates alone are a custom range, so older clients keep working.
        self.assertEqual(analytics.dashboard_period({"date_from": "2026-03-10", "date_to": "2026-03-10"})["key"], "custom")
        self.assertEqual(analytics.dashboard_period({})["key"], "today")

    def test_custom_range_validation(self):
        full_leap_year = period("custom", date(2026, 1, 1), date_from="2024-01-01", date_to="2024-12-31")
        self.assertEqual((full_leap_year["date_to"] - full_leap_year["date_from"]).days + 1, 366)
        for bad in (
            {"period": "custom"},
            {"period": "custom", "date_from": "2026-03-10"},
            {"period": "custom", "date_from": "2026-03-12", "date_to": "2026-03-10"},
            {"period": "custom", "date_from": "2026-02-30", "date_to": "2026-03-01"},
            {"period": "custom", "date_from": "10/03/2026", "date_to": "2026-03-12"},
            {"period": "custom", "date_from": "2025-01-01", "date_to": "2026-01-02"},
            {"period": "today", "date_from": "2026-03-10", "date_to": "2026-03-12"},
            {"period": "forever"},
        ):
            with self.subTest(bad=bad), self.assertRaises(analytics.ValidationError):
                analytics.dashboard_period(bad, today=date(2026, 10, 21))
        with self.assertRaises(analytics.ValidationError):
            analytics.dashboard_group_by({"group_by": "year"})
        self.assertEqual(analytics.dashboard_group_by({}), "day")

    def test_buckets_cover_partial_weeks_and_months(self):
        self.assertEqual(
            analytics.bucket_starts(date(2026, 10, 21), date(2026, 11, 2), "week"),
            [date(2026, 10, 19), date(2026, 10, 26), date(2026, 11, 2)],
        )
        self.assertEqual(
            analytics.bucket_starts(date(2025, 12, 15), date(2026, 2, 1), "month"),
            [date(2025, 12, 1), date(2026, 1, 1), date(2026, 2, 1)],
        )
        self.assertEqual(len(analytics.bucket_starts(date(2026, 2, 1), date(2026, 2, 28), "day")), 28)


class DashboardFinancialsTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = make_user(0, is_staff=True)
        self.api = APIClient()
        self.api.force_authenticate(self.admin)
        self.workers = iter(make_user(index) for index in range(1, 30))
        self.free = Service.objects.create(name="No cost", price_per_1000=Decimal("100.0000"))
        self.wallet = Wallet.objects.create(user=next(self.workers), balance=Decimal("0"))
        # A Task with no Order: its rewards are paid to users but belong to no Order's profit.
        self.standalone = Task.objects.create(target_link="https://example.com/s", total_slots=5, remaining_slots=5,
                                              reward_amount=Decimal("1.00000000"))

    def order_at(self, moment, quantity=2, status=Order.FulfillmentStatus.COMPLETED, approved=0, service=None):
        order = make_order(service or self.free, quantity)
        Order.objects.filter(pk=order.pk).update(created_at=moment, fulfillment_status=status)
        task = order.tasks.get()
        for _ in range(approved):
            claim = TaskClaim.objects.create(task=task, user=next(self.workers), status=TaskClaim.Status.APPROVED, reviewed_at=moment)
            self.credit(moment, Decimal("0.02500000"), task=task, claim=claim)
        return Order.objects.get(pk=order.pk)

    def credit(self, moment, amount, **links):
        links.setdefault("task", self.standalone)
        entry = WalletTransaction.objects.create(
            wallet=self.wallet, transaction_type=WalletTransaction.TransactionType.TASK_REWARD,
            direction=WalletTransaction.Direction.CREDIT, amount=amount, balance_after=amount, **links,
        )
        WalletTransaction.objects.filter(pk=entry.pk).update(created_at=moment)

    def payout(self, moment, amount, status=WithdrawalRequest.Status.APPROVED):
        return WithdrawalRequest.objects.create(
            user=self.wallet.user, amount=amount, payout_method="upi", payout_details="w@upi", status=status, reviewed_at=moment,
        )

    def test_month_boundaries_follow_india_time(self):
        # 23:59:59 IST on 31 March is still March; 00:00 IST on 1 April is April.
        self.order_at(utc(2026, 3, 31, 18, 29, 59))
        self.order_at(utc(2026, 3, 31, 18, 30, 0))
        self.order_at(utc(2026, 2, 28, 18, 30, 0))  # 00:00 IST on 1 March
        march = analytics.dashboard_financials(date(2026, 3, 1), date(2026, 3, 31))
        april = analytics.dashboard_financials(date(2026, 4, 1), date(2026, 4, 30))
        self.assertEqual(march["order_value"], "0.40000000")
        self.assertEqual(april["order_value"], "0.20000000")
        trend = analytics.dashboard_trend(date(2026, 3, 1), date(2026, 4, 30), "month")
        self.assertEqual([(row["bucket_start"], row["order_value"]) for row in trend], [("2026-03-01", "0.40000000"), ("2026-04-01", "0.20000000")])

    def test_figures_use_the_analytics_definitions(self):
        moment = utc(2026, 3, 10, 6)
        self.order_at(moment, quantity=4, approved=3)  # completed: revenue = 100 x 3 / 1000
        self.order_at(moment, quantity=2, status=Order.FulfillmentStatus.ACCEPTED)  # open: order value only
        self.credit(moment, Decimal("1.00000000"))  # a reward with no Order still counts as paid
        self.payout(moment, Decimal("2.50"))
        self.payout(moment, Decimal("9.00"), status=WithdrawalRequest.Status.REJECTED)
        current = analytics.dashboard_financials(date(2026, 3, 10), date(2026, 3, 10))
        self.assertEqual(current, {
            "order_value": "0.60000000",
            "recognized_revenue": "0.30000000",
            "paid_to_users": "1.07500000",
            "payments_made": "2.50000000",
            "profit": "0.22500000",
            "profit_available": True,
            "referral_bonuses_paid": "0.00000000",
        })
        start, end = analytics.period_bounds(date(2026, 3, 10), date(2026, 3, 10))
        summary = analytics.summary(start, end)
        self.assertEqual(
            (summary["recognized_revenue"], summary["paid_to_users"], summary["upi_paid_out"], summary["our_share"]),
            (current["recognized_revenue"], current["paid_to_users"], current["payments_made"], current["profit"]),
        )

    def test_cancelled_and_rejected_orders_are_excluded(self):
        moment = timezone.now()
        self.order_at(moment)
        self.order_at(moment, status=Order.FulfillmentStatus.CANCELLED)
        self.order_at(moment, status=Order.FulfillmentStatus.REJECTED)
        data = self.api.get(URL).data
        self.assertEqual(data["financials"]["current"]["order_value"], "0.20000000")
        self.assertEqual((data["orders"]["total"], data["orders"]["today"], data["orders"]["total_quantity"]), (1, 1, 2))
        self.assertEqual(data["trend"][0]["order_value"], "0.20000000")

    def test_profit_is_unavailable_when_a_closed_order_lacks_its_cost(self):
        moment = utc(2026, 3, 10, 6)
        costly = Service.objects.create(name="Costly", price_per_1000=Decimal("100.0000"), has_fulfillment_cost=True,
                                        fulfillment_cost_per_1000=Decimal("10.0000"))
        order = self.order_at(moment, approved=1, service=costly)
        Order.objects.filter(pk=order.pk).update(fulfillment_cost_per_1000_snapshot=None)
        current = analytics.dashboard_financials(date(2026, 3, 10), date(2026, 3, 10))
        self.assertIsNone(current["profit"])
        self.assertFalse(current["profit_available"])
        self.assertEqual(current["recognized_revenue"], "0.10000000")
        trend = analytics.dashboard_trend(date(2026, 3, 9), date(2026, 3, 10), "day")
        self.assertEqual([row["profit"] for row in trend], ["0.00000000", None])

    def test_services_without_a_cost_have_zero_cost_profit(self):
        self.order_at(timezone.now(), approved=2)
        current = self.api.get(URL).data["financials"]["current"]
        self.assertTrue(current["profit_available"])
        self.assertEqual(Decimal(current["profit"]), Decimal("0.2") - Decimal("0.05"))

    def test_trend_buckets_by_monday_weeks_with_empty_buckets_as_zeros(self):
        self.order_at(utc(2026, 10, 18, 20))  # Monday 19 Oct 01:30 IST, though still Sunday in UTC
        self.credit(utc(2026, 11, 2, 5), Decimal("0.50000000"))
        self.payout(utc(2026, 11, 3, 5), Decimal("1.00"))
        trend = analytics.dashboard_trend(date(2026, 10, 14), date(2026, 11, 4), "week")
        self.assertEqual([row["bucket_start"] for row in trend], ["2026-10-12", "2026-10-19", "2026-10-26", "2026-11-02"])
        zero = "0.00000000"
        self.assertEqual(trend[0], {"bucket_start": "2026-10-12", "order_value": zero, "paid_to_users": zero, "payments_made": zero, "profit": zero})
        self.assertEqual(trend[1]["order_value"], "0.20000000")
        self.assertEqual(trend[2]["order_value"], zero)
        self.assertEqual((trend[3]["paid_to_users"], trend[3]["payments_made"]), ("0.50000000", "1.00000000"))

    def test_endpoint_reports_current_and_previous_periods(self):
        today = timezone.localdate()
        noon = timezone.make_aware(datetime.combine(today, datetime.min.time())) + timedelta(hours=12)
        self.order_at(noon)
        self.order_at(noon - timedelta(days=1), quantity=4)
        response = self.api.get(URL, {"period": "today"})
        self.assertEqual(response.status_code, 200)
        data = response.data
        self.assertEqual(set(data), {"period", "currency", "financials", "trend", "users", "orders", "tasks", "reconciliation_alert"})
        self.assertEqual(data["period"]["label"], "Today")
        self.assertEqual(data["period"]["previous_from"], (today - timedelta(days=1)).isoformat())
        self.assertEqual(data["period"]["timezone"], "Asia/Kolkata")
        self.assertEqual(data["financials"]["current"]["order_value"], "0.20000000")
        self.assertEqual(data["financials"]["previous"]["order_value"], "0.40000000")
        self.assertEqual(len(data["trend"]), 1)

    def test_bad_parameters_are_rejected_and_workers_are_refused(self):
        for params in ({"period": "decade"}, {"group_by": "hour"}, {"period": "custom", "date_from": "2026-01-01", "date_to": "2027-06-01"}):
            with self.subTest(params=params):
                self.assertEqual(self.api.get(URL, params).status_code, 400)
        worker = APIClient()
        worker.force_authenticate(make_user(90))
        self.assertEqual(worker.get(URL).status_code, 403)


class DashboardUsersTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.admin = make_user(0, is_staff=True)
        self.api = APIClient()
        self.api.force_authenticate(self.admin)
        UserLevel.objects.all().delete()
        for order, (code, name, minimum, active) in enumerate([
            ("bronze", "Bronze", 1, True), ("silver", "Silver", 3, True), ("hidden", "Hidden", 4, False), ("gold", "Gold", 5, True),
        ], start=1):
            UserLevel.objects.create(code=code, name=name, minimum_approved_tasks=minimum, display_order=order,
                                     badge_color=f"#00000{order}", is_active=active)
        order = make_order(Service.objects.create(name="Levels", price_per_1000=Decimal("100.0000")), 50)
        self.task = order.tasks.get()

    def user_with(self, index, approved, **extra):
        user = make_user(index, **extra)
        for _ in range(approved):
            claim = TaskClaim.objects.create(task=self.task, user=user, status=TaskClaim.Status.APPROVED)
            TaskClaim.objects.filter(pk=claim.pk).update(created_at=timezone.now() - timedelta(days=30))
        return user

    def recent_claim(self, user, days_ago):
        claim = TaskClaim.objects.create(task=self.task, user=user, status=TaskClaim.Status.REJECTED)
        TaskClaim.objects.filter(pk=claim.pk).update(created_at=timezone.now() - timedelta(days=days_ago))

    def test_levels_bucket_like_the_strike_policy(self):
        counts = {1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 9}
        users = {index: self.user_with(index, approved) for index, approved in counts.items()}
        overview = analytics.compute_users_overview()
        by_code = {item["code"]: item["count"] for item in overview["by_level"]}
        self.assertEqual(by_code, {"bronze": 2, "silver": 2, "gold": 2})  # inactive "hidden" never appears
        self.assertEqual(overview["without_level"], 1)
        levels = active_levels()
        expected = {}
        for approved in counts.values():
            level = level_for_count(approved, levels)
            expected[level.code if level else None] = expected.get(level.code if level else None, 0) + 1
        self.assertEqual(expected, {None: 1, "bronze": 2, "silver": 2, "gold": 2})
        self.assertEqual(overview["by_level"][0], {"code": "bronze", "name": "Bronze", "badge_color": "#000001", "count": 2})
        listed = self.api.get("/api/v1/admin/users/", {"level": "silver"}).data["results"]
        self.assertEqual({row["id"] for row in listed}, {users[4].pk, users[5].pk})
        none = self.api.get("/api/v1/admin/users/", {"level": "none", "account_type": "worker"}).data["results"]
        self.assertEqual([row["id"] for row in none], [users[1].pk])
        self.assertEqual(self.api.get("/api/v1/admin/users/", {"level": "platinum"}).status_code, 400)

    def test_active_means_an_approved_account_with_a_claim_in_seven_days(self):
        active = self.user_with(1, 0)
        self.recent_claim(active, days_ago=6)
        stale = self.user_with(2, 0)
        self.recent_claim(stale, days_ago=8)
        idle = self.user_with(3, 0)
        pending = self.user_with(4, 0, approval_status="pending")
        self.recent_claim(pending, days_ago=1)
        self.user_with(5, 0, approval_status="disapproved")
        self.user_with(6, 0, approval_status="disabled")
        staff = self.user_with(7, 0, is_staff=True)
        self.recent_claim(staff, days_ago=1)

        overview = analytics.compute_users_overview()
        self.assertEqual(
            {key: overview[key] for key in ("total", "active", "inactive", "pending_approval", "disapproved", "disabled")},
            {"total": 6, "active": 1, "inactive": 2, "pending_approval": 1, "disapproved": 1, "disabled": 1},
        )
        listed = lambda **params: {row["id"] for row in self.api.get("/api/v1/admin/users/", {"account_type": "worker", **params}).data["results"]}
        self.assertEqual(listed(activity="active"), {active.pk})
        self.assertEqual(listed(activity="inactive"), {stale.pk, idle.pk})
        self.assertEqual(len(listed()), 6)
        self.assertEqual(self.api.get("/api/v1/admin/users/", {"activity": "sometimes"}).status_code, 400)

    def test_users_block_is_cached_for_a_minute(self):
        self.user_with(1, 0)
        first = self.api.get(URL).data["users"]["total"]
        self.user_with(2, 0)
        self.assertEqual(self.api.get(URL).data["users"]["total"], first)
        cache.delete(analytics.USERS_CACHE_KEY)
        self.assertEqual(self.api.get(URL).data["users"]["total"], first + 1)
