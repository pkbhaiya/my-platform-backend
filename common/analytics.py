"""Admin analytics, built only from existing records.

Orders, TaskClaims, the wallet ledger (WalletTransaction), WithdrawalRequests
and Wallets are read; nothing is stored. Money is summed exactly in Python
with Decimal, never with a database SUM (SQLite adds those as floats). Periods
are local (settings.TIME_ZONE) dates, used as half-open ranges
[date_from 00:00, date_to + 1 day 00:00).

Definitions:
- Order cohort: Orders created in the period.
- Recognized revenue: for closed cohort Orders (completed or partial),
  approved claims x price_per_1000_snapshot / 1000. A completed Order
  recognizes its whole total_order_value.
- Open order value: total_order_value of cohort Orders still accepted or in
  progress; shown separately, never counted as revenue.
- Paid to users: task_reward credits created in the period (by credit date).
- Our share: recognized revenue - rewards credited on those closed Orders -
  their recorded fulfillment cost for the delivered units; unavailable when
  any of them needs a cost that was never recorded.
- UPI paid out: approved withdrawals, by reviewed_at.
- Owed in wallets: the current sum of wallet balances (point in time); pending
  withdrawals, already deducted from those balances, are shown separately.
- Adjustment credits: a separate line, never part of rewards.

The Admin Dashboard (bottom of this module) reuses these definitions:
order value is the total_order_value of non-cancelled, non-rejected cohort
Orders; revenue, paid to users, payments made (UPI paid out) and profit (our
share) come from the same helpers as the Analytics summary.
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.cache import cache
from django.db.models import Count, Exists, OuterRef, Q
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError

from accounts.models import User
from accounts.strike_policy import active_levels, level_count_filters, no_level_filter, per_user_count
from orders.models import Order
from tasks.models import TaskClaim
from wallet.models import Wallet, WalletTransaction, WithdrawalRequest

CLOSED_ORDER_STATUSES = (Order.FulfillmentStatus.COMPLETED, Order.FulfillmentStatus.PARTIAL)
OPEN_ORDER_STATUSES = (Order.FulfillmentStatus.ACCEPTED, Order.FulfillmentStatus.IN_PROGRESS)
# Orders that earned nothing: left out of order value and every order count.
NON_REVENUE_ORDER_STATUSES = (Order.FulfillmentStatus.CANCELLED, Order.FulfillmentStatus.REJECTED)
PERIODS = ("this_month", "last_month", "last_3_months")
MONEY_PLACES = Decimal("0.00000001")


def money(value):
    """An exact fixed-point string with 8 places ("0.00000000", never "0E-8"), or None if unavailable."""
    return None if value is None else format(value.quantize(MONEY_PLACES), "f")


def exact_sum(values):
    return sum((value for value in values if value is not None), Decimal("0"))


def resolve_period(params):
    """(date_from, date_to) from ?period=... or ?date_from=&date_to=; defaults to this month."""
    today = timezone.localdate()
    period = params.get("period", "")
    date_from_raw, date_to_raw = params.get("date_from", ""), params.get("date_to", "")
    if period and (date_from_raw or date_to_raw):
        raise ValidationError({"period": "Send either a period or a date range, not both."})
    if date_from_raw or date_to_raw:
        date_from, date_to = parse_date(date_from_raw or ""), parse_date(date_to_raw or "")
        if not date_from or not date_to or date_from > date_to:
            raise ValidationError({"date_range": "Use valid YYYY-MM-DD dates with date_from no later than date_to."})
        return date_from, date_to
    period = period or "this_month"
    if period not in PERIODS:
        raise ValidationError({"period": f"Choose one of: {', '.join(PERIODS)}."})
    first_of_month = today.replace(day=1)
    if period == "this_month":
        return first_of_month, today
    if period == "last_month":
        last_day = first_of_month - timedelta(days=1)
        return last_day.replace(day=1), last_day
    # The current month and the two before it, through today.
    month_index = today.year * 12 + today.month - 1 - 2
    return date(month_index // 12, month_index % 12 + 1, 1), today


def period_bounds(date_from, date_to):
    return (
        timezone.make_aware(datetime.combine(date_from, time.min)),
        timezone.make_aware(datetime.combine(date_to + timedelta(days=1), time.min)),
    )


def in_period(field, start, end):
    return Q(**{f"{field}__gte": start, f"{field}__lt": end})


def reward_credits():
    return WalletTransaction.objects.filter(
        transaction_type=WalletTransaction.TransactionType.TASK_REWARD,
        direction=WalletTransaction.Direction.CREDIT,
    )


def rewards_paid(start, end):
    """Paid to users: task_reward credits created in the period."""
    return reward_credits().filter(in_period("created_at", start, end))


def referral_bonuses_paid(start, end):
    """Referral bonus credits whose referral was paid in the period (kept apart from task rewards)."""
    return WalletTransaction.objects.filter(
        transaction_type=WalletTransaction.TransactionType.REFERRAL_BONUS,
        direction=WalletTransaction.Direction.CREDIT,
        referral__paid_at__gte=start,
        referral__paid_at__lt=end,
    )


def approved_payouts(start, end):
    """UPI paid out (payments made): approved withdrawals, by reviewed_at."""
    return WithdrawalRequest.objects.filter(in_period("reviewed_at", start, end), status=WithdrawalRequest.Status.APPROVED)


def orders_with_delivery():
    """Orders annotated with `delivered` = approved claims (derived, never stored)."""
    return Order.objects.select_related("service").annotate(
        delivered=Count("tasks__claims", filter=Q(tasks__claims__status=TaskClaim.Status.APPROVED), distinct=True),
    )


def rewards_by_order(order_ids):
    totals = {}
    for order_id, amount in reward_credits().filter(task__order_id__in=order_ids).values_list("task__order_id", "amount"):
        totals[order_id] = totals.get(order_id, Decimal("0")) + amount
    return totals


def order_figures(order, rewards):
    """Revenue, rewards, cost and our share for one Order (None where not applicable)."""
    closed = order.fulfillment_status in CLOSED_ORDER_STATUSES
    revenue = order.price_per_1000_snapshot * order.delivered / Decimal("1000") if closed else None
    cost = order.fulfillment_cost_for(order.delivered) if closed else None
    share = revenue - rewards - cost if closed and cost is not None else None
    return {"revenue": revenue, "rewards": rewards, "cost": cost, "share": share, "closed": closed}


def closed_orders(orders):
    return [order for order in orders if order.fulfillment_status in CLOSED_ORDER_STATUSES]


def cohort_profit(closed, rewards):
    """Recognized revenue, rewards, cost and our share of closed cohort Orders.

    The one implementation of these figures, shared by the Analytics summary,
    the Dashboard financials and every Dashboard trend bucket. ``rewards`` maps
    Order id to credited rewards. Cost and share are None when any Order needs
    a fulfillment cost that was never recorded.
    """
    figures = [order_figures(order, rewards.get(order.pk, Decimal("0"))) for order in closed]
    cost_known = all(item["cost"] is not None for item in figures)
    revenue = exact_sum(item["revenue"] for item in figures)
    rewards_total = exact_sum(item["rewards"] for item in figures)
    cost = exact_sum(item["cost"] for item in figures) if cost_known else None
    return {
        "revenue": revenue,
        "rewards": rewards_total,
        "cost": cost,
        "share": revenue - rewards_total - cost if cost_known else None,
    }


def summary(start, end):
    cohort = list(orders_with_delivery().filter(in_period("created_at", start, end)))
    closed = closed_orders(cohort)
    figures = cohort_profit(closed, rewards_by_order([order.pk for order in closed]))
    pending = WithdrawalRequest.objects.filter(status=WithdrawalRequest.Status.PENDING).values_list("amount", flat=True)
    pending = list(pending)
    return {
        "recognized_revenue": money(figures["revenue"]),
        "closed_orders": len(closed),
        "open_order_value": money(exact_sum(order.total_order_value for order in cohort if order.fulfillment_status in OPEN_ORDER_STATUSES)),
        "open_orders": sum(order.fulfillment_status in OPEN_ORDER_STATUSES for order in cohort),
        "paid_to_users": money(exact_sum(rewards_paid(start, end).values_list("amount", flat=True))),
        "rewards_on_closed_orders": money(figures["rewards"]),
        "fulfillment_cost": money(figures["cost"]),
        "our_share": money(figures["share"]),
        "our_share_available": figures["share"] is not None,
        "upi_paid_out": money(exact_sum(approved_payouts(start, end).values_list("amount", flat=True))),
        "referral_bonuses_paid": money(exact_sum(referral_bonuses_paid(start, end).values_list("amount", flat=True))),
        "owed_in_wallets": money(exact_sum(Wallet.objects.values_list("balance", flat=True))),
        "pending_withdrawals": money(exact_sum(pending)),
        "pending_withdrawal_count": len(pending),
        "adjustment_credits": money(exact_sum(
            WalletTransaction.objects.filter(
                in_period("created_at", start, end),
                transaction_type=WalletTransaction.TransactionType.ADJUSTMENT,
                direction=WalletTransaction.Direction.CREDIT,
            ).values_list("amount", flat=True)
        )),
    }


def order_rows(orders):
    """Orders tab rows for one page of annotated Orders."""
    rewards = rewards_by_order([order.pk for order in orders])
    rows = []
    for order in orders:
        figures = order_figures(order, rewards.get(order.pk, Decimal("0")))
        rows.append({
            "id": order.pk,
            "service_id": order.service_id,
            "service_name": order.service.name,
            "admin_wallet_id": order.admin_wallet_id,
            "quantity": order.quantity,
            "delivered": order.delivered,
            "status": order.fulfillment_status,
            "status_display": order.get_fulfillment_status_display(),
            "order_value": money(order.total_order_value),
            "recognized_revenue": money(figures["revenue"]),
            "paid_to_users": money(figures["rewards"]),
            "fulfillment_cost": money(figures["cost"]),
            "our_share": money(figures["share"]),
            "created_at": order.created_at.isoformat(),
        })
    return rows


def order_approvals(order):
    """Approved claims on one Order with the reward credited for each."""
    claims = TaskClaim.objects.filter(task__order=order, status=TaskClaim.Status.APPROVED).select_related("user").order_by("reviewed_at", "id")
    credited = {}
    for claim_id, amount in reward_credits().filter(task__order=order).values_list("claim_id", "amount"):
        credited[claim_id] = credited.get(claim_id, Decimal("0")) + amount
    return [{
        "claim_id": claim.pk,
        "user_id": claim.user_id,
        "username": claim.user.platform_username,
        "approved_at": claim.reviewed_at.isoformat() if claim.reviewed_at else None,
        "amount": money(credited.get(claim.pk)),
    } for claim in claims]


def active_users(start, end):
    """Non-staff users with period activity, or a balance still owed to them."""
    credit = rewards_paid(start, end).filter(wallet__user=OuterRef("pk"))
    payout = approved_payouts(start, end).filter(user=OuterRef("pk"))
    approval = TaskClaim.objects.filter(in_period("reviewed_at", start, end), user=OuterRef("pk"), status=TaskClaim.Status.APPROVED)
    return (
        User.objects.filter(is_staff=False, is_superuser=False)
        .filter(Exists(credit) | Exists(payout) | Exists(approval) | Q(wallet__balance__gt=0))
        .select_related("wallet")
        .order_by("id")
    )


def user_rows(users, start, end):
    ids = [user.pk for user in users]
    approved, earned, withdrawn = {}, {}, {}
    for user_id in TaskClaim.objects.filter(in_period("reviewed_at", start, end), user_id__in=ids, status=TaskClaim.Status.APPROVED).values_list("user_id", flat=True):
        approved[user_id] = approved.get(user_id, 0) + 1
    for user_id, amount in rewards_paid(start, end).filter(wallet__user_id__in=ids).values_list("wallet__user_id", "amount"):
        earned[user_id] = earned.get(user_id, Decimal("0")) + amount
    for user_id, amount in approved_payouts(start, end).filter(user_id__in=ids).values_list("user_id", "amount"):
        withdrawn[user_id] = withdrawn.get(user_id, Decimal("0")) + amount
    rows = []
    for user in users:
        wallet = getattr(user, "wallet", None)
        rows.append({
            "user_id": user.pk,
            "username": user.platform_username,
            "approved_tasks": approved.get(user.pk, 0),
            "earned": money(earned.get(user.pk, Decimal("0"))),
            "withdrawn": money(withdrawn.get(user.pk, Decimal("0"))),
            "balance": money(wallet.balance if wallet else Decimal("0")),
        })
    return rows


def payouts(start, end):
    """Withdrawals requested or reviewed in the period."""
    return (
        WithdrawalRequest.objects.filter(in_period("requested_at", start, end) | in_period("reviewed_at", start, end))
        .select_related("user")
        .order_by("-requested_at", "-id")
    )


def payout_row(withdrawal):
    approved = withdrawal.status == WithdrawalRequest.Status.APPROVED
    return {
        "id": withdrawal.pk,
        "user_id": withdrawal.user_id,
        "username": withdrawal.user.platform_username,
        "amount": money(withdrawal.amount),
        "upi_id": withdrawal.payout_details if withdrawal.payout_method == WithdrawalRequest.PayoutMethod.UPI else None,
        "requested_at": withdrawal.requested_at.isoformat(),
        "approved_at": withdrawal.reviewed_at.isoformat() if approved and withdrawal.reviewed_at else None,
        "status": withdrawal.status,
        "status_display": withdrawal.get_status_display(),
    }


# ---------------------------------------------------------------------------
# Admin Dashboard: period financials, trend and user counts
# ---------------------------------------------------------------------------

DASHBOARD_PERIODS = {
    "today": "Today",
    "yesterday": "Yesterday",
    "this_week": "This week",
    "last_week": "Last week",
    "this_month": "This month",
    "last_month": "Last month",
    "custom": "Custom range",
}
GROUP_BY = ("day", "week", "month")
MAX_RANGE_DAYS = 366
ACTIVE_WINDOW = timedelta(days=7)
USERS_CACHE_KEY = "admin-dashboard-users"
USERS_CACHE_SECONDS = 60


def shift_month(day, months):
    """The first day of the month ``months`` away from ``day``'s month."""
    index = day.year * 12 + day.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def month_end(first_day):
    return shift_month(first_day, 1) - timedelta(days=1)


def parse_range_date(raw):
    try:
        return parse_date(raw or "")
    except ValueError:
        return None


def dashboard_period(params, today=None):
    """The selected period and the one it is compared with (local, inclusive dates).

    Weeks start on Monday. To-date periods (today, this week, this month) are
    compared with the same span of the previous day, week or month; whole
    periods with the whole previous one; a custom range with the same number of
    days immediately before it. Dates alone (no period) mean a custom range.
    """
    today = today or timezone.localdate()
    raw_from, raw_to = params.get("date_from", ""), params.get("date_to", "")
    key = params.get("period", "") or ("custom" if raw_from or raw_to else "today")
    if key not in DASHBOARD_PERIODS:
        raise ValidationError({"period": f"Choose one of: {', '.join(DASHBOARD_PERIODS)}."})
    if key != "custom" and (raw_from or raw_to):
        raise ValidationError({"period": "date_from and date_to are only used with period=custom."})

    if key in ("today", "yesterday"):
        date_from = date_to = today - timedelta(days=0 if key == "today" else 1)
        previous_from = previous_to = date_from - timedelta(days=1)
    elif key in ("this_week", "last_week"):
        monday = today - timedelta(days=today.weekday())
        date_from, date_to = (monday, today) if key == "this_week" else (monday - timedelta(days=7), monday - timedelta(days=1))
        previous_from, previous_to = date_from - timedelta(days=7), date_to - timedelta(days=7)
    elif key == "this_month":
        date_from, date_to = today.replace(day=1), today
        previous_from = shift_month(today, -1)
        previous_to = previous_from.replace(day=min(today.day, month_end(previous_from).day))
    elif key == "last_month":
        date_from = shift_month(today, -1)
        date_to = month_end(date_from)
        previous_from = shift_month(today, -2)
        previous_to = month_end(previous_from)
    else:
        date_from, date_to = parse_range_date(raw_from), parse_range_date(raw_to)
        if not date_from or not date_to or date_from > date_to:
            raise ValidationError({"date_range": "Use valid YYYY-MM-DD dates with date_from no later than date_to."})
        days = (date_to - date_from).days + 1
        if days > MAX_RANGE_DAYS:
            raise ValidationError({"date_range": f"Choose a range of at most {MAX_RANGE_DAYS} days."})
        previous_to = date_from - timedelta(days=1)
        previous_from = previous_to - timedelta(days=days - 1)

    return {
        "key": key,
        "label": DASHBOARD_PERIODS[key],
        "date_from": date_from,
        "date_to": date_to,
        "previous_from": previous_from,
        "previous_to": previous_to,
    }


def dashboard_group_by(params):
    group_by = params.get("group_by", "") or "day"
    if group_by not in GROUP_BY:
        raise ValidationError({"group_by": f"Choose one of: {', '.join(GROUP_BY)}."})
    return group_by


def bucket_start(day, group_by):
    if group_by == "week":
        return day - timedelta(days=day.weekday())
    if group_by == "month":
        return day.replace(day=1)
    return day


def bucket_starts(date_from, date_to, group_by):
    """Every bucket touching the range, empty ones included (Monday weeks, calendar months)."""
    starts, current = [], bucket_start(date_from, group_by)
    while current <= date_to:
        starts.append(current)
        current = shift_month(current, 1) if group_by == "month" else current + timedelta(days=7 if group_by == "week" else 1)
    return starts


def order_value(orders):
    """Order value: total_order_value of Orders that were not cancelled or rejected."""
    return exact_sum(order.total_order_value for order in orders if order.fulfillment_status not in NON_REVENUE_ORDER_STATUSES)


def dashboard_figures(orders, rewards, paid, payments, referral_bonuses=()):
    """The Dashboard figures for one period or trend bucket, from rows already fetched."""
    profit = cohort_profit(closed_orders(orders), rewards)
    return {
        # Its own line: not part of paid_to_users (task rewards) or of profit.
        "referral_bonuses_paid": money(exact_sum(referral_bonuses)),
        "order_value": money(order_value(orders)),
        "recognized_revenue": money(profit["revenue"]),
        "paid_to_users": money(exact_sum(paid)),
        "payments_made": money(exact_sum(payments)),
        "profit": money(profit["share"]),
        "profit_available": profit["share"] is not None,
    }


def dashboard_financials(date_from, date_to):
    start, end = period_bounds(date_from, date_to)
    orders = list(orders_with_delivery().filter(in_period("created_at", start, end)))
    return dashboard_figures(
        orders,
        rewards_by_order([order.pk for order in closed_orders(orders)]),
        rewards_paid(start, end).values_list("amount", flat=True),
        approved_payouts(start, end).values_list("amount", flat=True),
        referral_bonuses_paid(start, end).values_list("amount", flat=True),
    )


def dashboard_trend(date_from, date_to, group_by):
    """One row per bucket; each source is read once for the whole range, then bucketed by local date."""
    start, end = period_bounds(date_from, date_to)

    def bucket_of(moment):
        return bucket_start(timezone.localtime(moment).date(), group_by)

    orders, paid, payments = defaultdict(list), defaultdict(list), defaultdict(list)
    cohort = list(orders_with_delivery().filter(in_period("created_at", start, end)))
    for order in cohort:
        orders[bucket_of(order.created_at)].append(order)
    for moment, amount in rewards_paid(start, end).values_list("created_at", "amount"):
        paid[bucket_of(moment)].append(amount)
    for moment, amount in approved_payouts(start, end).values_list("reviewed_at", "amount"):
        payments[bucket_of(moment)].append(amount)
    rewards = rewards_by_order([order.pk for order in closed_orders(cohort)])

    rows = []
    for bucket in bucket_starts(date_from, date_to, group_by):
        figures = dashboard_figures(orders[bucket], rewards, paid[bucket], payments[bucket])
        rows.append({
            "bucket_start": bucket.isoformat(),
            "order_value": figures["order_value"],
            "paid_to_users": figures["paid_to_users"],
            "payments_made": figures["payments_made"],
            "profit": figures["profit"],
        })
    return rows


def worker_accounts():
    return User.objects.filter(is_staff=False, is_superuser=False)


def recent_claim(since):
    """Whether the outer User created a claim since ``since`` (the "active" rule)."""
    return Exists(TaskClaim.objects.filter(user=OuterRef("pk"), created_at__gte=since))


def compute_users_overview(now=None):
    """Non-staff account counts in one aggregate query, levels included.

    Active: Approved accounts with a claim created in the last 7 days; Inactive:
    Approved accounts without one. Levels use the strike-policy thresholds.
    """
    since = (now or timezone.now()) - ACTIVE_WINDOW
    levels = active_levels()
    statuses = User.ApprovalStatus
    approved = Q(approval_status=statuses.APPROVED)
    aggregates = {
        "total": Count("pk"),
        "active": Count("pk", filter=approved & Q(has_recent_claim=True)),
        "inactive": Count("pk", filter=approved & Q(has_recent_claim=False)),
        "pending_approval": Count("pk", filter=Q(approval_status=statuses.PENDING)),
        "disapproved": Count("pk", filter=Q(approval_status=statuses.DISAPPROVED)),
        "disabled": Count("pk", filter=Q(approval_status=statuses.DISABLED)),
    }
    buckets = level_count_filters(levels)
    for index, (_level, condition) in enumerate(buckets):
        aggregates[f"level_{index}"] = Count("pk", filter=condition)
    if levels:
        aggregates["without_level"] = Count("pk", filter=no_level_filter(levels))
    counts = worker_accounts().annotate(
        approved_count=per_user_count(TaskClaim.objects.filter(status=TaskClaim.Status.APPROVED)),
        has_recent_claim=recent_claim(since),
    ).aggregate(**aggregates)
    return {
        "total": counts["total"],
        "active": counts["active"],
        "inactive": counts["inactive"],
        "pending_approval": counts["pending_approval"],
        "disapproved": counts["disapproved"],
        "disabled": counts["disabled"],
        "by_level": [
            {"code": level.code, "name": level.name, "badge_color": level.badge_color, "count": counts[f"level_{index}"]}
            for index, (level, _condition) in enumerate(buckets)
        ],
        "without_level": counts.get("without_level", counts["total"]),
        "active_window_days": ACTIVE_WINDOW.days,
    }


def users_overview():
    """User counts do not depend on the period, so they are cached for a minute."""
    return cache.get_or_set(USERS_CACHE_KEY, compute_users_overview, USERS_CACHE_SECONDS)
