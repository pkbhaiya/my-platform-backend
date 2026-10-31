from datetime import datetime, time, timedelta
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Case, Count, F, IntegerField, Q, Sum, When
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import CanViewOwnRecords
from accounts.strike_policy import strike_summary
from common import analytics
from wallet.reconciliation import unreviewed_reconciliation_alert
from orders.models import Order

from .models import Task, TaskClaim, TaskStrike
from .services import (
    TaskStateConflict,
    approve_submitted_task,
    cancel_task,
    reject_submitted_task,
    resume_task,
    submitted_result_label,
)


# Statuses an aggregate Task can be in (the per-unit legacy statuses are never set).
LISTED_TASK_STATUSES = (
    Task.Status.AVAILABLE, Task.Status.FULLY_CLAIMED, Task.Status.PAUSED,
    Task.Status.COMPLETED, Task.Status.CANCELLED, Task.Status.UNAVAILABLE,
)


# Orders that earned nothing: left out of every dashboard count and figure.
NON_REVENUE_ORDER_STATUSES = analytics.NON_REVENUE_ORDER_STATUSES


def cancellation(task):
    """Why and by whom a cancelled Task was cancelled (select ``cancelled_by``); else None."""
    if task.status != Task.Status.CANCELLED:
        return None
    return {
        "reason": task.cancel_reason,
        "reason_display": task.get_cancel_reason_display(),
        "note": task.cancel_note,
        "cancelled_at": task.cancelled_at.isoformat() if task.cancelled_at else None,
        "cancelled_by": task.cancelled_by.email if task.cancelled_by_id else None,
    }


def delivery(quantity, delivered):
    """Delivered = approved claims (derived, never stored); remains = quantity - delivered."""
    return {"delivered": delivered, "remains": max(0, quantity - delivered)}


class AdminPendingTasksAPIView(APIView):
    """List submitted tasks awaiting admin review."""

    permission_classes = [IsAdminUser]

    class Pagination(PageNumberPagination):
        page_size = 20
        page_size_query_param = "page_size"
        max_page_size = 100

    def get(self, request):
        claims = TaskClaim.objects.filter(status=TaskClaim.Status.WORK_SUBMITTED).select_related(
            "task", "task__order", "task__order__service", "user"
        ).order_by("id")

        paginator = self.Pagination()
        page = paginator.paginate_queryset(claims, request, view=self)
        results = [
            {
                "id": claim.id,
                "task_id": claim.task_id,
                "task_name": claim.task.display_name,
                "order_id": claim.task.order_id,
                "user_id": claim.user_id,
                "user_email": claim.user.email,
                "platform_username": claim.user.platform_username,
                "target_link": claim.task.target_link,
                "reward_amount": str(claim.task.reward_amount),
                "status": claim.status,
                "submitted_result": claim.submitted_result,
                "submitted_result_display": submitted_result_label(claim),
                "submission_note": claim.submission_note,
                "submitted_at": (
                    claim.submitted_at.isoformat()
                    if claim.submitted_at
                    else None
                ),
            }
            for claim in page
        ]
        return paginator.get_paginated_response(results)


class AdminTaskReviewAPIView(APIView):
    """Allow administrators to approve or reject submitted tasks."""

    permission_classes = [IsAdminUser]

    def post(self, request, task_id):
        pending = TaskClaim.objects.filter(task_id=task_id, status=TaskClaim.Status.WORK_SUBMITTED)
        if pending.count() != 1:
            return Response({"detail": "This task has multiple or no pending submissions. Review an individual submission instead."}, status=status.HTTP_409_CONFLICT)
        return self._review(request, pending.get().pk)

    def _review(self, request, claim_id):
        action = request.data.get("action")
        if action not in ("approve", "reject"):
            return Response(
                {"detail": "Action must be 'approve' or 'reject'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            if action == "approve":
                task = approve_submitted_task(
                    task_id=claim_id,
                    reviewer=request.user,
                )
            else:
                task = reject_submitted_task(
                    task_id=claim_id,
                    reviewer=request.user,
                )
        except DjangoValidationError as exc:
            raise ValidationError({"detail": exc.messages})

        payload = {"id": task.id, "task_id": task.task_id, "status": task.status}
        if action == "reject":
            # Lets the reviewer see when this strike disapproved the account.
            payload["account_status"] = task.account_status
        return Response(payload)


class AdminTaskSubmissionReviewAPIView(AdminTaskReviewAPIView):
    """Review one user's submission, identified by its claim record."""

    def post(self, request, claim_id):
        if not TaskClaim.objects.filter(pk=claim_id).exists():
            raise NotFound("Task submission not found.")
        return self._review(request, claim_id)


class AdminTaskReviewHistoryAPIView(APIView):
    """List tasks reviewed by an administrator."""

    permission_classes = [IsAdminUser]

    class Pagination(PageNumberPagination):
        page_size = 20
        page_size_query_param = "page_size"
        max_page_size = 100

    def get(self, request):
        tasks = TaskClaim.objects.filter(reviewed_at__isnull=False).select_related(
            "task", "task__order", "task__order__service", "user", "reviewed_by"
        ).order_by("-reviewed_at", "-id")

        paginator = self.Pagination()
        page = paginator.paginate_queryset(tasks, request, view=self)
        results = [
            {
                "id": task.id,
                "task_id": task.task_id,
                "task_name": task.task.display_name,
                "order_id": task.task.order_id,
                "user_id": task.user_id,
                "user_email": task.user.email,
                "platform_username": task.user.platform_username,
                "status": task.status,
                "reward_amount": str(task.task.reward_amount),
                "reviewed_by": (task.reviewed_by.email if task.reviewed_by else None),
                "submitted_at": (
                    task.submitted_at.isoformat() if task.submitted_at else None
                ),
                "reviewed_at": task.reviewed_at.isoformat(),
            }
            for task in page
        ]
        return paginator.get_paginated_response(results)


class AdminDashboardStatsAPIView(APIView):
    """Admin Dashboard: period financials vs the previous period, a trend, user counts, orders and tasks.

    GET /api/v1/admin/dashboard/stats/?period=today|yesterday|this_week|last_week|this_month|last_month|custom
        &date_from=&date_to= (custom only, at most 366 days)&group_by=day|week|month

    Every financial figure comes from ``common.analytics``, the same definitions
    as the Analytics page. Dates are local (settings.TIME_ZONE), weeks start on
    Monday, and cancelled and rejected Orders are excluded everywhere.
    """

    permission_classes = [IsAdminUser]

    def get(self, request):
        period = analytics.dashboard_period(request.query_params)
        group_by = analytics.dashboard_group_by(request.query_params)
        today_start, tomorrow_start = analytics.period_bounds(timezone.localdate(), timezone.localdate())

        revenue_orders = Order.objects.exclude(fulfillment_status__in=NON_REVENUE_ORDER_STATUSES)
        order_totals = revenue_orders.aggregate(
            total=Count("id"),
            today=Count("id", filter=Q(created_at__gte=today_start, created_at__lt=tomorrow_start)),
            quantity=Sum("quantity"),
        )
        available = Q(status=Task.Status.AVAILABLE, remaining_slots__gt=0)
        task_totals = Task.objects.aggregate(
            total=Count("id"),
            available=Count("id", filter=available),
            remaining_capacity=Sum("remaining_slots", filter=available),
        )

        return Response({
            "period": {
                "key": period["key"],
                "label": period["label"],
                "date_from": period["date_from"].isoformat(),
                "date_to": period["date_to"].isoformat(),
                "previous_from": period["previous_from"].isoformat(),
                "previous_to": period["previous_to"].isoformat(),
                "group_by": group_by,
                "timezone": settings.TIME_ZONE,
            },
            "currency": "INR",
            "financials": {
                "current": analytics.dashboard_financials(period["date_from"], period["date_to"]),
                "previous": analytics.dashboard_financials(period["previous_from"], period["previous_to"]),
            },
            "trend": analytics.dashboard_trend(period["date_from"], period["date_to"], group_by),
            "users": analytics.users_overview(),
            # Unreviewed wallet reconciliation mismatches; the Dashboard shows a red banner while set.
            "reconciliation_alert": unreviewed_reconciliation_alert(),
            "orders": {
                "total": order_totals["total"],
                "today": order_totals["today"],
                "total_quantity": order_totals["quantity"] or 0,
            },
            "tasks": {
                # Aggregate Task rows (one per Order), not claim slots.
                "total": task_totals["total"],
                "available": task_totals["available"],
                "remaining_capacity": task_totals["remaining_capacity"] or 0,
                # Claim records currently held by users and not yet submitted.
                "in_progress_claims": TaskClaim.objects.filter(status=TaskClaim.Status.CLAIMED).count(),
            },
        })


class AdminOrdersPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


class AdminOrdersAPIView(APIView):
    """List orders with optional status filtering and pagination."""

    permission_classes = [IsAdminUser]
    pagination_class = AdminOrdersPagination

    def get(self, request):
        orders = Order.objects.select_related("service").prefetch_related("tasks__cancelled_by").annotate(
            task_count=Count("tasks", distinct=True),
            delivered=Count("tasks__claims", filter=Q(tasks__claims__status=TaskClaim.Status.APPROVED), distinct=True),
        ).order_by("-created_at")

        request_key = request.query_params.get("idempotency_key", "").strip()
        if request_key:
            try:
                request_key = UUID(request_key)
            except (TypeError, ValueError):
                return Response(
                    {"detail": "Invalid idempotency_key."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            orders = orders.filter(idempotency_key=request_key)

        fulfillment_status = request.query_params.get("fulfillment_status")

        valid_statuses = {choice[0] for choice in Order.FulfillmentStatus.choices}

        if fulfillment_status:
            if fulfillment_status not in valid_statuses:
                return Response(
                    {
                        "detail": "Invalid fulfillment_status.",
                        "valid_statuses": sorted(valid_statuses),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            orders = orders.filter(fulfillment_status=fulfillment_status)

        date_from = request.query_params.get("date_from")
        date_to = request.query_params.get("date_to")

        if date_from:
            parsed_from = parse_date(date_from)
            if not parsed_from:
                return Response(
                    {"detail": "Invalid date_from. Use YYYY-MM-DD."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            start_datetime = timezone.make_aware(
                datetime.combine(parsed_from, time.min)
            )
            orders = orders.filter(created_at__gte=start_datetime)

        if date_to:
            parsed_to = parse_date(date_to)
            if not parsed_to:
                return Response(
                    {"detail": "Invalid date_to. Use YYYY-MM-DD."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            end_datetime = timezone.make_aware(
                datetime.combine(parsed_to + timedelta(days=1), time.min)
            )
            orders = orders.filter(created_at__lt=end_datetime)

        if date_from and date_to and parsed_from > parsed_to:
            return Response(
                {"detail": "date_from must be before or equal to date_to."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        search = request.query_params.get("search", "").strip()

        if search:
            search_filter = Q(target_link__icontains=search) | Q(
                external_order_id__icontains=search
            )

            if search.isdigit():
                search_filter |= Q(id=int(search))

            orders = orders.filter(search_filter)

        paginator = self.pagination_class()
        page = paginator.paginate_queryset(orders, request, view=self)

        def first_task(order):
            # Prefetched, so this reads the cache rather than querying.
            tasks = order.tasks.all()
            return tasks[0] if tasks else None

        results = [
            {
                "id": order.id,
                "service_id": order.service_id,
                "service_name": order.service.name,
                "source": order.source,
                "external_order_id": order.external_order_id,
                "idempotency_key": str(order.idempotency_key) if order.idempotency_key else None,
                "target_link": order.target_link,
                "quantity": order.quantity,
                "total_order_value": str(order.total_order_value),
                "availability_status": order.availability_status,
                "fulfillment_status": order.fulfillment_status,
                "task_count": order.task_count,
                "task_id": task.id if task else None,
                "total_slots": task.total_slots if task else None,
                "remaining_slots": task.remaining_slots if task else None,
                "task_status": task.status if task else None,
                **delivery(order.quantity, order.delivered),
                "cancellation": cancellation(task) if task else None,
                "created_at": order.created_at.isoformat(),
                "updated_at": order.updated_at.isoformat(),
            }
            for order, task in ((order, first_task(order)) for order in page)
        ]

        return paginator.get_paginated_response(results)


class AdminTaskStrikesAPIView(APIView):
    """Read-only, paginated strike history for administrators."""

    permission_classes = [IsAdminUser]

    class Pagination(PageNumberPagination):
        page_size = 20
        page_size_query_param = "page_size"
        max_page_size = 100

    def get(self, request):
        strikes = TaskStrike.objects.select_related(
            "user", "task", "task__order", "issued_by"
        ).order_by("-created_at", "-id")
        user_id = request.query_params.get("user_id", "")
        task_id = request.query_params.get("task_id", "")
        if user_id:
            if not user_id.isdigit() or int(user_id) < 1:
                raise ValidationError({"user_id": "Enter a positive user ID."})
            strikes = strikes.filter(user_id=int(user_id))
        if task_id:
            if not task_id.isdigit() or int(task_id) < 1:
                raise ValidationError({"task_id": "Enter a positive task ID."})
            strikes = strikes.filter(task_id=int(task_id))
        paginator = self.Pagination()
        page = paginator.paginate_queryset(strikes, request, view=self)
        results = [{
            "id": strike.id,
            "user_id": strike.user_id,
            "user_email": strike.user.email,
            "task_id": strike.task_id,
            "order_id": strike.task.order_id if strike.task_id else None,
            "issued_by_email": strike.issued_by.email,
            "reason": strike.reason,
            "created_at": strike.created_at.isoformat(),
        } for strike in page]
        return paginator.get_paginated_response(results)


class AdminTaskHistoryAPIView(APIView):
    """Paginated aggregate Task rows; each row's claims load via its submissions endpoint."""

    permission_classes = [IsAdminUser]

    class Pagination(PageNumberPagination):
        page_size = 20
        page_size_query_param = "page_size"
        max_page_size = 100

    def get(self, request):
        tasks = Task.objects.select_related("order", "order__service", "cancelled_by").annotate(
            total_claims=Count("claims", distinct=True),
            target_not_available_reports=Count(
                "claims", filter=Q(claims__status=TaskClaim.Status.POST_NOT_AVAILABLE), distinct=True,
            ),
            total_submissions=Count("claims", filter=Q(claims__submitted_at__isnull=False), distinct=True),
            pending_reviews=Count("claims", filter=Q(claims__status=TaskClaim.Status.WORK_SUBMITTED), distinct=True),
            approved_submissions=Count("claims", filter=Q(claims__status=TaskClaim.Status.APPROVED), distinct=True),
            rejected_submissions=Count("claims", filter=Q(claims__status=TaskClaim.Status.REJECTED), distinct=True),
        ).annotate(
            # Computed from the aggregate, so it never multiplies rows per claim.
            _pending_first=Case(When(pending_reviews__gt=0, then=0), default=1, output_field=IntegerField()),
            # Paused Tasks wait for an admin's Keep or Cancel decision, so they lead.
            _paused_first=Case(When(status=Task.Status.PAUSED, then=0), default=1, output_field=IntegerField()),
        ).order_by("_paused_first", "_pending_first", "-created_at", "-id")

        task_status = request.query_params.get("status", "")
        if task_status:
            if task_status not in LISTED_TASK_STATUSES:
                raise ValidationError({"status": f"Choose one of: {', '.join(LISTED_TASK_STATUSES)}."})
            tasks = tasks.filter(status=task_status)

        claim_filter = Q()
        user_id = request.query_params.get("user_id", "")
        if user_id:
            if not user_id.isdigit() or int(user_id) < 1:
                raise ValidationError({"user_id": "Enter a positive user ID."})
            claim_filter &= Q(user_id=int(user_id))
        username = request.query_params.get("username", "").strip()
        if username:
            claim_filter &= Q(user__platform_username__icontains=username)
        if claim_filter:
            tasks = tasks.filter(pk__in=TaskClaim.objects.filter(claim_filter).values("task_id"))

        date_from = request.query_params.get("date_from", "")
        date_to = request.query_params.get("date_to", "")
        parsed_from = parse_date(date_from) if date_from else None
        parsed_to = parse_date(date_to) if date_to else None
        if date_from and not parsed_from:
            raise ValidationError({"date_from": "Use YYYY-MM-DD."})
        if date_to and not parsed_to:
            raise ValidationError({"date_to": "Use YYYY-MM-DD."})
        if parsed_from and parsed_to and parsed_from > parsed_to:
            raise ValidationError({"date_to": "date_from must be before or equal to date_to."})
        if parsed_from:
            tasks = tasks.filter(created_at__gte=timezone.make_aware(datetime.combine(parsed_from, time.min)))
        if parsed_to:
            tasks = tasks.filter(created_at__lt=timezone.make_aware(datetime.combine(parsed_to + timedelta(days=1), time.min)))

        search = request.query_params.get("search", "").strip()
        if search:
            claim_matches = TaskClaim.objects.filter(
                Q(user__email__icontains=search) | Q(user__platform_username__icontains=search)
            ).values("task_id")
            search_query = (
                Q(title__icontains=search)
                | Q(target_link__icontains=search)
                | Q(order__service__name__icontains=search)
                | Q(pk__in=claim_matches)
            )
            if search.isdigit():
                search_query |= Q(pk=int(search)) | Q(order_id=int(search))
            tasks = tasks.filter(search_query)

        paginator = self.Pagination()
        page = paginator.paginate_queryset(tasks, request, view=self)
        results = [{
            "id": task.id,
            "task_name": task.display_name,
            "order_id": task.order_id,
            "order_status": task.order.fulfillment_status if task.order_id else None,
            "service_id": task.order.service_id if task.order_id else None,
            "service_name": task.order.service.name if task.order_id else None,
            "target_link": task.target_link,
            "total_slots": task.total_slots,
            "remaining_slots": task.remaining_slots,
            "status": task.status,
            "status_display": task.get_status_display(),
            "reward_amount": str(task.reward_amount),
            "total_claims": task.total_claims,
            "total_submissions": task.total_submissions,
            "pending_reviews": task.pending_reviews,
            "approved_submissions": task.approved_submissions,
            "rejected_submissions": task.rejected_submissions,
            "target_not_available_reports": task.target_not_available_reports,
            **delivery(task.total_slots, task.approved_submissions),
            "cancellation": cancellation(task),
            # Share of order value per claim slot (reporting only; not the user reward).
            "allocated_order_value_per_claim": (
                str(task.order.total_order_value / task.total_slots)
                if task.order_id and task.total_slots else None
            ),
        } for task in page]
        return paginator.get_paginated_response(results)


class AdminTaskSubmissionsAPIView(APIView):
    """Every claim on one Task, grouped for review.

    Groups, in this order: submitted and pending review; submitted and finished
    (approved, rejected, or Target Not Available); claimed but not submitted. The
    grouping comes from each claim's own persisted status, never from the Task's.
    """

    permission_classes = [IsAdminUser]
    GROUPS = ("pending_review", "reviewed", "not_submitted")

    class Pagination(PageNumberPagination):
        page_size = 20
        page_size_query_param = "page_size"
        max_page_size = 100

    @staticmethod
    def group_expression():
        finished = [TaskClaim.Status.APPROVED, TaskClaim.Status.REJECTED, TaskClaim.Status.POST_NOT_AVAILABLE]
        return Case(
            When(status=TaskClaim.Status.WORK_SUBMITTED, then=0),
            When(Q(status__in=finished) | Q(submitted_at__isnull=False) | Q(reviewed_at__isnull=False), then=1),
            default=2,
            output_field=IntegerField(),
        )

    def get(self, request, task_id):
        task = Task.objects.select_related("order", "order__service", "cancelled_by").filter(pk=task_id).first()
        if not task:
            return Response({"detail": "Task not found."}, status=status.HTTP_404_NOT_FOUND)
        claims = TaskClaim.objects.filter(task_id=task_id).select_related("user", "reviewed_by").annotate(
            _group=self.group_expression(),
        ).order_by("_group", F("submitted_at").asc(nulls_last=True), "id")
        group_counts = dict.fromkeys(self.GROUPS, 0)
        # order_by() clears the row ordering so it does not split the GROUP BY.
        for row in claims.order_by().values("_group").annotate(total=Count("id")):
            group_counts[self.GROUPS[row["_group"]]] = row["total"]
        paginator = self.Pagination()
        page = paginator.paginate_queryset(claims, request, view=self)
        results = [{
            "id": claim.id,
            "task_id": claim.task_id,
            "user_id": claim.user_id,
            "user_email": claim.user.email,
            "platform_username": claim.user.platform_username,
            "review_group": self.GROUPS[claim._group],
            "submitted": claim.submitted_at is not None,
            "submitted_result": claim.submitted_result,
            "submitted_result_display": submitted_result_label(claim),
            "submission_note": claim.submission_note,
            "status": claim.status,
            "status_display": claim.get_status_display(),
            "can_review": claim.status == TaskClaim.Status.WORK_SUBMITTED,
            "reviewed_at": claim.reviewed_at.isoformat() if claim.reviewed_at else None,
            "reviewed_by": claim.reviewed_by.email if claim.reviewed_by else None,
        } for claim in page]
        response = paginator.get_paginated_response(results)
        response.data["group_counts"] = group_counts
        response.data["task"] = {
            "id": task.id,
            "task_name": task.display_name,
            "order_id": task.order_id,
            "service_name": task.order.service.name if task.order_id else None,
            "target_link": task.target_link,
            "total_slots": task.total_slots,
            "remaining_slots": task.remaining_slots,
            "status": task.status,
            "status_display": task.get_status_display(),
            "reward_amount": str(task.reward_amount),
            "total_claims": sum(group_counts.values()),
            "target_not_available_reports": claims.filter(status=TaskClaim.Status.POST_NOT_AVAILABLE).count(),
            **delivery(task.total_slots, claims.filter(status=TaskClaim.Status.APPROVED).count()),
            "cancellation": cancellation(task),
        }
        return response


class MyTaskReviewStatusAPIView(APIView):
    """Return only the authenticated user's strike and rejected-task summary."""

    # Not for accounts awaiting approval; disapproved accounts still see their strikes.
    permission_classes = [IsAuthenticated, CanViewOwnRecords]

    def get(self, request):
        user_tasks = TaskClaim.objects.filter(user=request.user)
        summary = strike_summary(request.user)
        latest_rejection = (
            user_tasks.filter(status=TaskClaim.Status.REJECTED)
            .order_by("-reviewed_at", "-id")
            .values("id", "task_id", "task__title", "task__target_link", "review_note", "reviewed_at")
            .first()
        )
        if latest_rejection:
            claim = user_tasks.select_related("task__order", "task__order__service").get(pk=latest_rejection["id"])
            latest_rejection["id"] = latest_rejection["task_id"]
            latest_rejection["task_name"] = claim.task.display_name
            latest_rejection["reviewed_at"] = claim.reviewed_at.isoformat() if claim.reviewed_at else None
        # The user's one active claim (unique per user), so the panel indicator can open exactly it.
        active = user_tasks.filter(status=TaskClaim.Status.CLAIMED).select_related(
            "task__order__service").order_by("-claimed_at").first()
        return Response({
            "active_claim": {
                "claim_id": active.pk,
                "task_id": active.task_id,
                "task_name": active.task.display_name,
                "target_opened": active.target_opened_at is not None,
            } if active else None,
            "approval_status": request.user.approval_status,
            "status_reason": request.user.status_reason,
            "strike_count": summary["strike_count"],
            "strike_limit": summary["strike_limit"],
            "strike_limit_reached": summary["strike_limit_reached"],
            "current_level": summary["current_level"],
            "rejected_task_count": user_tasks.filter(status=TaskClaim.Status.REJECTED).count(),
            "latest_rejected_task": latest_rejection,
        })


def task_state(task):
    """A Task's state after an admin Keep or Cancel action, with its Order."""
    task = Task.objects.select_related("order", "cancelled_by").get(pk=task.pk)
    delivered = task.claims.filter(status=TaskClaim.Status.APPROVED).count()
    return {
        "id": task.id,
        "status": task.status,
        "status_display": task.get_status_display(),
        "total_slots": task.total_slots,
        "remaining_slots": task.remaining_slots,
        **delivery(task.total_slots, delivered),
        "cancellation": cancellation(task),
        "order": {
            "id": task.order_id,
            "quantity": task.order.quantity,
            "fulfillment_status": task.order.fulfillment_status,
            "availability_status": task.order.availability_status,
            **delivery(task.order.quantity, delivered),
        } if task.order_id else None,
    }


def run_task_action(action):
    try:
        task = action()
    except TaskStateConflict as exc:
        return Response({"detail": exc.messages}, status=status.HTTP_409_CONFLICT)
    except DjangoValidationError as exc:
        raise ValidationError({"detail": exc.messages})
    return Response(task_state(task))


class AdminTaskResumeAPIView(APIView):
    """Keep a paused Task: POST /api/v1/admin/tasks/<id>/resume/ (idempotent)."""

    permission_classes = [IsAdminUser]

    def post(self, request, task_id):
        get_object_or_404(Task, pk=task_id)
        return run_task_action(lambda: resume_task(task_id=task_id, reviewer=request.user))


class TaskCancelSerializer(serializers.Serializer):
    # `expired` is set only by the order-expiry sweep.
    reason = serializers.ChoiceField(choices=[choice for choice in Task.CancelReason.choices if choice[0] != Task.CancelReason.EXPIRED])
    note = serializers.CharField(required=False, allow_blank=True, max_length=2000, default="")

    def validate(self, attrs):
        if attrs["reason"] == Task.CancelReason.OTHER and not attrs["note"].strip():
            raise serializers.ValidationError({"note": "Add a note when the reason is Other."})
        return attrs


class AdminTaskCancelAPIView(APIView):
    """Cancel a Task: POST /api/v1/admin/tasks/<id>/cancel/ with reason and note (idempotent)."""

    permission_classes = [IsAdminUser]

    def post(self, request, task_id):
        get_object_or_404(Task, pk=task_id)
        serializer = TaskCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return run_task_action(lambda: cancel_task(task_id=task_id, reviewer=request.user, **serializer.validated_data))
