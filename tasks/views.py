from datetime import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Case, Count, Exists, F, OuterRef, Q, Sum, When
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.exceptions import ValidationError
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.permissions import IsApprovedUser
from wallet.models import WalletTransaction

from orders.duplicates import hide_tasks_already_worked

from .models import Task, TaskClaim, TaskUserClaim
from .serializers import AvailableTaskSerializer, TaskClaimSerializer, TaskSubmissionSerializer
from .services import claim_task, expire_stale_claims, get_accessible_level_codes, open_task_target, submit_task_result


class AvailableTasksPagination(PageNumberPagination):
    page_size = 5
    page_size_query_param = "page_size"
    max_page_size = 5


class AvailableTasksAPIView(generics.ListAPIView):
    """List tasks available to approved users."""

    serializer_class = AvailableTaskSerializer
    pagination_class = AvailableTasksPagination
    permission_classes = [IsAuthenticated, IsApprovedUser]

    def get_queryset(self):
        # Expired claims free their slots before the list is read.
        expire_stale_claims()
        accessible_codes = get_accessible_level_codes(self.request.user)
        level_filter = Q(order__isnull=True) | Q(order__service__eligible_level="all")
        if accessible_codes:
            level_filter |= Q(order__service__eligible_level__in=accessible_codes)
        # Permanent per-user identity rows: a Task never returns after this user claimed it.
        already_claimed = TaskUserClaim.objects.filter(task_id=OuterRef("pk"), user=self.request.user)
        tasks = Task.objects.filter(status=Task.Status.AVAILABLE, remaining_slots__gt=0).filter(
            level_filter
        ).annotate(_claimed_by_user=Exists(already_claimed)).filter(
            _claimed_by_user=False
        ).select_related("order", "order__service").order_by(
            # Oldest order first (ties by id); Tasks without an order after them, oldest first.
            F("order__created_at").asc(nulls_last=True),
            Case(When(order__isnull=True, then="created_at")).asc(),
            "id",
        )
        # Nor a Task for a link and service this user already worked on (any claim, any status).
        tasks = hide_tasks_already_worked(tasks, self.request.user)
        # Search covers only what the card shows; the target link is never searchable here.
        search = self.request.query_params.get("search", "").strip()[:100]
        if search:
            tasks = tasks.filter(
                Q(title__icontains=search)
                | Q(order__service__name__icontains=search)
                | Q(order__service__description__icontains=search)
            )
        return tasks


class ClaimTaskAPIView(APIView):
    """Claim one available task."""

    permission_classes = [IsAuthenticated, IsApprovedUser]

    def post(self, request, task_id):
        try:
            task = claim_task(
                task_id=task_id,
                user=request.user,
            )
        except DjangoValidationError as exc:
            raise ValidationError({"detail": exc.messages})

        claim = TaskClaim.objects.select_related("task__order__service").get(pk=task.claim_id)
        return Response(
            TaskClaimSerializer(claim).data,
            status=status.HTTP_200_OK,
        )


class OpenTaskTargetAPIView(APIView):
    permission_classes = [IsAuthenticated, IsApprovedUser]

    def post(self, request, task_id):
        try:
            result = open_task_target(task_id=task_id, user=request.user)
        except DjangoValidationError as exc:
            raise ValidationError({"detail": exc.messages})
        # Same Asia/Kolkata representation as the My Tasks serializer.
        return Response({key: timezone.localtime(value).isoformat() if isinstance(value, datetime) else value
            for key, value in result.items()})


class SubmitTaskResultAPIView(APIView):
    """Submit work done or report an unavailable post."""

    permission_classes = [IsAuthenticated, IsApprovedUser]

    def post(self, request, task_id):
        serializer = TaskSubmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            task = submit_task_result(
                task_id=task_id,
                user=request.user,
                result=serializer.validated_data["result"],
                note=serializer.validated_data.get("note", ""),
            )
        except DjangoValidationError as exc:
            raise ValidationError({"detail": exc.messages})

        claim = TaskClaim.objects.select_related("task__order__service").get(pk=task.claim_id)
        return Response(
            TaskClaimSerializer(claim).data,
            status=status.HTTP_200_OK,
        )


class MyTasksAPIView(generics.ListAPIView):
    """List tasks assigned to the logged-in user."""

    serializer_class = TaskClaimSerializer
    permission_classes = [IsAuthenticated, IsApprovedUser]

    def get_queryset(self):
        # A claim past its time limit expires here instead of being listed as active.
        expire_stale_claims()
        return TaskClaim.objects.filter(user=self.request.user, status=TaskClaim.Status.CLAIMED).select_related(
            "task", "task__order", "task__order__service").order_by("-claimed_at")


class UserTaskHistoryAPIView(APIView):
    permission_classes = [IsAuthenticated, IsApprovedUser]

    class Pagination(PageNumberPagination):
        page_size = 20
        page_size_query_param = "page_size"
        max_page_size = 100

    def get(self, request):
        claims = TaskClaim.objects.filter(user=request.user).exclude(
            status=TaskClaim.Status.CLAIMED
        ).select_related("task", "task__order", "task__order__service").order_by("-updated_at", "-id")
        claim_status = request.query_params.get("status", "")
        if claim_status:
            allowed = set(TaskClaim.Status.values) - {TaskClaim.Status.CLAIMED}
            if claim_status not in allowed:
                raise ValidationError({"status": f"Choose one of: {', '.join(sorted(allowed))}."})
            claims = claims.filter(status=claim_status)
        paginator = self.Pagination()
        page = paginator.paginate_queryset(claims, request, view=self)
        return paginator.get_paginated_response(TaskClaimSerializer(page, many=True).data)


class MyTaskSummaryAPIView(APIView):
    """Worker dashboard counters, computed only from the signed-in user's own records."""

    permission_classes = [IsAuthenticated, IsApprovedUser]

    def get(self, request):
        counts = dict(
            TaskClaim.objects.filter(user=request.user).order_by()
            .values_list("status").annotate(total=Count("id"))
        )
        earned = WalletTransaction.objects.filter(
            wallet__user=request.user,
            transaction_type=WalletTransaction.TransactionType.TASK_REWARD,
            direction=WalletTransaction.Direction.CREDIT,
        ).aggregate(total=Sum("amount"))["total"] or Decimal("0")
        return Response({
            "claims_by_status": {value: counts.get(value, 0) for value in TaskClaim.Status.values},
            "total_claims": sum(counts.values()),
            "approved": counts.get(TaskClaim.Status.APPROVED, 0),
            "pending_review": counts.get(TaskClaim.Status.WORK_SUBMITTED, 0),
            "rejected": counts.get(TaskClaim.Status.REJECTED, 0),
            "in_progress": counts.get(TaskClaim.Status.CLAIMED, 0),
            "total_earned": f"{earned:.8f}",
            "currency": "INR",
        })
