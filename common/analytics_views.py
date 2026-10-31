from django.conf import settings
from django.shortcuts import get_object_or_404
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from . import analytics


class AnalyticsPagination(PageNumberPagination):
    page_size = 20
    max_page_size = 20


class AnalyticsView(APIView):
    permission_classes = [IsAdminUser]

    def period(self, request):
        date_from, date_to = analytics.resolve_period(request.query_params)
        start, end = analytics.period_bounds(date_from, date_to)
        period = {"date_from": date_from.isoformat(), "date_to": date_to.isoformat(), "timezone": settings.TIME_ZONE}
        return period, start, end

    def page(self, request, queryset, build_rows, period):
        paginator = AnalyticsPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        response = paginator.get_paginated_response(build_rows(page))
        response.data["period"] = period
        return response


class AdminAnalyticsSummaryAPIView(AnalyticsView):
    """GET /api/v1/admin/analytics/?period=this_month|last_month|last_3_months or ?date_from=&date_to="""

    def get(self, request):
        period, start, end = self.period(request)
        return Response({"period": period, "currency": "INR", **analytics.summary(start, end)})


class AdminAnalyticsOrdersAPIView(AnalyticsView):
    """Orders created in the period; ?admin_wallet=<id> for one admin wallet's API orders, ?admin_wallet=manual for manual orders."""

    def get(self, request):
        period, start, end = self.period(request)
        orders = analytics.orders_with_delivery().filter(analytics.in_period("created_at", start, end)).order_by("-created_at", "-id")
        admin_wallet = request.query_params.get("admin_wallet", "")
        if admin_wallet == "manual":
            orders = orders.filter(admin_wallet__isnull=True)
        elif admin_wallet:
            if not admin_wallet.isdigit():
                raise ValidationError({"admin_wallet": "Use an admin wallet ID, or manual."})
            orders = orders.filter(admin_wallet_id=int(admin_wallet))
        response = self.page(request, orders, analytics.order_rows, period)
        response.data["admin_wallet"] = admin_wallet or None
        return response


class AdminAnalyticsOrderDetailAPIView(AnalyticsView):
    def get(self, request, order_id):
        order = get_object_or_404(analytics.orders_with_delivery(), pk=order_id)
        return Response({"order": analytics.order_rows([order])[0], "approved_claims": analytics.order_approvals(order)})


class AdminAnalyticsUsersAPIView(AnalyticsView):
    def get(self, request):
        period, start, end = self.period(request)
        return self.page(request, analytics.active_users(start, end), lambda page: analytics.user_rows(page, start, end), period)


class AdminAnalyticsPayoutsAPIView(AnalyticsView):
    def get(self, request):
        period, start, end = self.period(request)
        return self.page(request, analytics.payouts(start, end), lambda page: [analytics.payout_row(item) for item in page], period)
