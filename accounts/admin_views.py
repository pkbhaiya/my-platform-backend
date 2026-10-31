from decimal import Decimal

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.generics import ListAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework.views import APIView

from common import analytics
from tasks.models import TaskClaim, TaskStrike
from wallet.models import Wallet, WalletTransaction, WithdrawalRequest

from .constants import LEGACY_GENDER_LABELS, STATE_NAMES
from .models import DuplicateRegistrationFlag, User, UserChangeLog, UserLevel
from .passwords import may_set_password, password_changes, set_password_by_admin
from .serializers import (
    AdminSetPasswordSerializer, AdminUserCorrectionSerializer, AdminUserLevelSerializer, AdminUserListSerializer,
    AdminUserSerializer, AdminUserStatusSerializer,
)
from .identity import has_linked_accounts, linked_accounts
from .strike_policy import active_levels, level_count_filters, no_level_filter, per_user_count, strike_summary
from referrals.admin_views import user_referrals
from tasks.services import recent_target_not_available_rates


class AdminUserStatusAPIView(APIView):
    """Approve or disable a regular user account."""

    permission_classes = [IsAdminUser]

    @transaction.atomic
    def patch(self, request, user_id):
        # Locked so a concurrent strike-limit disapproval cannot interleave.
        user = get_object_or_404(User.objects.select_for_update(), pk=user_id)

        # Protect administrator accounts from this endpoint.
        if user.is_staff or user.is_superuser:
            return Response(
                {"detail": "Administrator accounts cannot be changed here."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = AdminUserStatusSerializer(user, data=request.data)
        serializer.is_valid(raise_exception=True)

        new_status = serializer.validated_data["approval_status"]
        if new_status == user.approval_status:
            return Response(AdminUserSerializer(user).data)

        # Disapproved users keep their login; only Disabled locks the account.
        # Strikes are never touched here, so a reapproved user keeps their
        # history and the next eligible rejection is evaluated as usual.
        user.approval_status = new_status
        user.is_active = new_status != User.ApprovalStatus.DISABLED
        user.status_reason = (
            serializer.validated_data.get("reason")
            or f"{user.get_approval_status_display()} by an administrator."
        )
        user.status_change_source = User.StatusChangeSource.ADMIN
        user.status_changed_by = request.user
        user.status_changed_at = timezone.now()

        user.save(update_fields=[
            "approval_status", "is_active", "status_reason",
            "status_change_source", "status_changed_by", "status_changed_at",
        ])

        return Response(AdminUserSerializer(user).data)


class AdminUserPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100


# The only User columns the list reads (AdminUserListSerializer).
LIST_COLUMNS = ("id", "platform_username", "approval_status", "date_joined", "is_staff", "is_superuser", "is_active")


class AdminUserListAPIView(ListAPIView):
    """One page of users with only the table's columns: a COUNT, the page and the levels (no query per row).

    Strikes and approved claims are correlated subqueries on the page query;
    everything else (email, WhatsApp, state, referrals, linked accounts,
    earnings, ...) is in AdminUserDetailAPIView.
    """

    serializer_class = AdminUserListSerializer
    permission_classes = [IsAdminUser]
    pagination_class = AdminUserPagination

    def get_serializer_context(self):
        context = super().get_serializer_context()
        if "all_levels" not in context:
            # One query for both: the active levels (as active_levels() sorts them) and all levels.
            all_levels = list(UserLevel.objects.all())
            context["all_levels"] = all_levels
            context["levels"] = sorted((level for level in all_levels if level.is_active),
                                       key=lambda level: (level.minimum_approved_tasks, level.display_order))
        return context

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        return self.get_paginated_response(self.get_serializer(page, many=True).data)

    def get_queryset(self):
        queryset = User.objects.only(*LIST_COLUMNS).annotate(
            strike_total=per_user_count(TaskStrike.objects.all()),
            approved_count=per_user_count(TaskClaim.objects.filter(status=TaskClaim.Status.APPROVED)),
        ).order_by("-date_joined", "-id")

        approval_status = self.request.query_params.get("approval_status")

        if approval_status:
            if approval_status not in User.ApprovalStatus.values:
                raise ValidationError(
                    {
                        "approval_status": (
                            "Invalid status. Choose pending, approved, disapproved, or disabled."
                        )
                    }
                )

            queryset = queryset.filter(approval_status=approval_status)

        # The Dashboard's user chips link here with these filters; they use the
        # same rules as its counts (common.analytics.compute_users_overview).
        account_type = self.request.query_params.get("account_type", "")
        if account_type:
            if account_type not in ("worker", "staff"):
                raise ValidationError({"account_type": "Choose worker or staff."})
            staff = Q(is_staff=True) | Q(is_superuser=True)
            queryset = queryset.exclude(staff) if account_type == "worker" else queryset.filter(staff)

        activity = self.request.query_params.get("activity", "")
        if activity:
            if activity not in ("active", "inactive"):
                raise ValidationError({"activity": "Choose active or inactive."})
            recent = analytics.recent_claim(timezone.now() - analytics.ACTIVE_WINDOW)
            queryset = queryset.filter(approval_status=User.ApprovalStatus.APPROVED).annotate(has_recent_claim=recent).filter(
                has_recent_claim=activity == "active"
            )

        level_code = self.request.query_params.get("level", "")
        if level_code:
            levels = active_levels()
            buckets = {level.code: condition for level, condition in level_count_filters(levels)}
            if level_code in buckets:
                queryset = queryset.filter(buckets[level_code])
            elif level_code == "none":
                queryset = queryset.filter(no_level_filter(levels))
            else:
                raise ValidationError({"level": "Unknown level. Use an active level code or none."})

        linked = self.request.query_params.get("has_linked_accounts", "")
        if linked:
            if linked not in ("true", "false"):
                raise ValidationError({"has_linked_accounts": "Use true or false."})
            queryset = queryset.filter(has_linked_accounts()) if linked == "true" else queryset.exclude(has_linked_accounts())

        state = self.request.query_params.get("state", "")
        if state:
            if state == "none":
                queryset = queryset.filter(Q(state__isnull=True) | Q(state=""))
            elif state in STATE_NAMES:
                queryset = queryset.filter(state=state)
            else:
                raise ValidationError({"state": "Use a state code or none."})

        search = self.request.query_params.get("search", "").strip()
        if search:
            search_filter = (Q(email__icontains=search) | Q(platform_username__icontains=search)
                             | Q(referral_code=search.upper()))
            if search.isdigit():
                search_filter |= Q(pk=int(search))
            queryset = queryset.filter(search_filter)

        return queryset


def linked_account_rows(user):
    """Accounts sharing a hashed IP address or device with ``user``: who, and which kind (never the value)."""
    links = linked_accounts([user.pk])[user.pk]
    others = {other.pk: other for other in User.objects.filter(pk__in=links)}
    return [{
        "id": other_id,
        "email": others[other_id].email,
        "platform_username": others[other_id].platform_username,
        "approval_status": others[other_id].approval_status,
        "shared": sorted(kinds),
    } for other_id, kinds in sorted(links.items()) if other_id in others]


def referred_by(user):
    """Who referred ``user`` (one Referral at most), or None."""
    from referrals.models import Referral

    try:
        referral = user.referral  # select_related by the list view
    except Referral.DoesNotExist:
        return None
    return {
        "referral_id": referral.pk,
        "user_id": referral.referrer_id,
        "username": referral.referrer.platform_username,
        "status": referral.status,
        "status_display": referral.get_status_display(),
        "created_at": referral.created_at.isoformat(),
        "paid_at": referral.paid_at.isoformat() if referral.paid_at else None,
    }


CORRECTABLE_FIELDS = ("state", "gender")


def field_label(field, value):
    if not value:
        return None
    if field == "state":
        return STATE_NAMES.get(value, value)
    return dict(User.Gender.choices).get(value) or LEGACY_GENDER_LABELS.get(value) or value


def profile_changes(user, limit=10):
    """The latest changes to ``user``'s state and gender: the user's one-time state, or an admin correction."""
    rows = UserChangeLog.objects.filter(user=user, field__in=CORRECTABLE_FIELDS).select_related("changed_by")[:limit]
    return [{
        "field": change.field,
        "old": change.old_value or None,
        "old_display": field_label(change.field, change.old_value),
        "new": change.new_value,
        "new_display": field_label(change.field, change.new_value),
        "changed_by": change.changed_by.email if change.changed_by_id else None,
        "by_user": change.changed_by_id == user.pk,
        "changed_at": change.changed_at.isoformat(),
    } for change in rows]


def correctable_details(user):
    return {
        "state": user.state,
        "state_display": user.get_state_display() if user.state else None,
        "gender": user.gender,
        "gender_display": user.gender_label,
        "gender_needs_update": user.gender_needs_update,
        "profile_changes": profile_changes(user),
    }


class AdminUserProfileAPIView(APIView):
    """PATCH /api/v1/admin/users/<id>/profile/ {"state"?, "gender"?}: an admin correction.

    Users cannot change these after sign-up. Each changed field is recorded in UserChangeLog.
    """

    permission_classes = [IsAdminUser]

    def patch(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        serializer = AdminUserCorrectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            locked = User.objects.select_for_update().get(pk=user.pk)
            changes = {field: value for field, value in serializer.validated_data.items() if getattr(locked, field) != value}
            for field, value in changes.items():
                UserChangeLog.objects.create(user=locked, field=field, old_value=getattr(locked, field) or "", new_value=value, changed_by=request.user)
            if changes:
                User.objects.filter(pk=locked.pk).update(**changes)
        user.refresh_from_db()
        return Response(correctable_details(user))


EIGHT_PLACES = Decimal("0.00000001")


def money8(value):
    return format(Decimal(value or 0).quantize(EIGHT_PLACES), "f")


def earnings(user):
    """Worker wallet figures for the detail view: balance, credits by kind, withdrawals by status."""
    wallet = Wallet.objects.filter(user=user).values_list("balance", flat=True).first()
    credits = dict(WalletTransaction.objects.filter(wallet__user=user, direction=WalletTransaction.Direction.CREDIT)
                   .values("transaction_type").annotate(total=Sum("amount")).values_list("transaction_type", "total"))
    withdrawals = dict(WithdrawalRequest.objects.filter(user=user).values("status").annotate(total=Sum("amount"))
                       .values_list("status", "total"))
    return {
        "wallet_balance": money8(wallet),
        "task_rewards": money8(credits.get(WalletTransaction.TransactionType.TASK_REWARD)),
        "referral_bonuses": money8(credits.get(WalletTransaction.TransactionType.REFERRAL_BONUS)),
        "withdrawn": money8(withdrawals.get(WithdrawalRequest.Status.APPROVED)),
        "pending_withdrawals": money8(withdrawals.get(WithdrawalRequest.Status.PENDING)),
    }


def platform_username_rows(user):
    return [{"platform": row.platform_name, "username": row.username, "added_at": row.created_at.isoformat()}
            for row in user.additional_platform_usernames.all()]


class AdminUserDetailAPIView(APIView):
    """Return safe details for a single user to administrators (everything the list leaves out)."""

    permission_classes = [IsAdminUser]

    def get(self, request, user_id):
        user = get_object_or_404(User.objects.select_related("status_changed_by"), pk=user_id)
        summary = strike_summary(user)

        return Response(
            {
                "id": user.id,
                "approved_task_count": summary["approved_task_count"],
                "current_level": summary["current_level"],
                "strike_count": summary["strike_count"],
                "strike_limit": summary["strike_limit"],
                "strike_limit_source": summary["strike_limit_source"],
                "strike_limit_reached": summary["strike_limit_reached"],
                "status_reason": user.status_reason,
                "status_change_source": user.status_change_source,
                "status_changed_by": user.status_changed_by.email if user.status_changed_by else None,
                "status_changed_at": user.status_changed_at.isoformat() if user.status_changed_at else None,
                "email": user.email,
                "full_name": user.full_name,
                "platform_username": user.platform_username,
                "whatsapp_number": user.whatsapp_number,
                **correctable_details(user),
                "approval_status": user.approval_status,
                "is_active": user.is_active,
                "is_staff": user.is_staff,
                "is_superuser": user.is_superuser,
                "date_joined": user.date_joined.isoformat(),
                "last_login": (
                    user.last_login.isoformat() if user.last_login else None
                ),
                # NULL key: this account shared its username with an earlier
                # one before usernames had to be unique; review it.
                "platform_username_unclaimed": user.platform_username_key is None,
                "upi_id": user.upi_id,
                "upi_id_changed_at": user.upi_id_changed_at.isoformat() if user.upi_id_changed_at else None,
                "linked_accounts": linked_account_rows(user),
                "referral_code": user.referral_code,
                "platform_usernames": platform_username_rows(user),
                "target_not_available_rate": recent_target_not_available_rates([user.pk])[user.pk],
                "earnings": earnings(user),
                "password_changes": password_changes(user),
                "referred_by": referred_by(user),
                "referrals_made": user.referrals_made.count(),
                "referrals": user_referrals(user),
            }
        )


@method_decorator(sensitive_post_parameters("new_password", "confirm_password"), name="dispatch")
class AdminUserSetPasswordAPIView(APIView):
    """POST /api/v1/admin/users/<id>/set-password/ {new_password, confirm_password}.

    Any admin may set a worker's (or their own) password; another admin's or
    staff account's needs a superuser. The user is signed out of every device
    (sessions ended, refresh tokens blacklisted, access tokens invalid) and the
    change is audit-logged. The password is never returned, logged or stored
    in plain text; the old one is never needed.
    """

    permission_classes = [IsAdminUser]

    def post(self, request, user_id):
        target = get_object_or_404(User, pk=user_id)
        if not may_set_password(request.user, target):
            return Response({"detail": "Only a superuser can change another administrator's password."},
                            status=status.HTTP_403_FORBIDDEN)
        serializer = AdminSetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            result = set_password_by_admin(target=target, new_password=serializer.validated_data["new_password"], actor=request.user)
        except DjangoValidationError as exc:
            raise ValidationError({"new_password": exc.messages})
        return Response({"detail": "Password updated. The user has been logged out of all devices.", **result})


class AdminUserActivityAPIView(APIView):
    """Show a user's task activity and actual wallet rewards."""

    permission_classes = [IsAdminUser]

    def get(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)

        # One query for every claim counter instead of one query each.
        claim_counts = TaskClaim.objects.filter(user=user).aggregate(
            total_assigned=Count("pk"),
            **{
                key: Count("pk", filter=Q(status=claim_status))
                for key, claim_status in (
                    ("claimed", TaskClaim.Status.CLAIMED),
                    ("pending_review", TaskClaim.Status.WORK_SUBMITTED),
                    ("approved", TaskClaim.Status.APPROVED),
                    ("rejected", TaskClaim.Status.REJECTED),
                    ("post_not_available", TaskClaim.Status.POST_NOT_AVAILABLE),
                    ("expired", TaskClaim.Status.EXPIRED),
                    ("cancelled", TaskClaim.Status.CANCELLED),
                    ("unavailable", TaskClaim.Status.UNAVAILABLE),
                )
            },
        )

        wallet = Wallet.objects.filter(user=user).first()

        reward_total = WalletTransaction.objects.filter(
            wallet__user=user,
            transaction_type=(WalletTransaction.TransactionType.TASK_REWARD),
            direction=WalletTransaction.Direction.CREDIT,
        ).aggregate(total=Sum("amount"))["total"]

        reward_total = reward_total or Decimal("0.00000000")

        return Response(
            {
                "user": {
                    "id": user.id,
                    "email": user.email,
                    "full_name": user.full_name,
                    "approval_status": user.approval_status,
                },
                "tasks": claim_counts,
                "wallet": {
                    "balance": str(wallet.balance if wallet else Decimal("0.00000000")),
                    "total_task_rewards_credited": format(reward_total, ".8f"),
                },
            }
        )


class AdminUserTasksAPIView(APIView):
    """List an individual user's tasks for admin review."""

    permission_classes = [IsAdminUser]

    def get(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)

        tasks = TaskClaim.objects.filter(user=user).select_related(
            "task", "task__order", "task__order__service"
        ).order_by("-created_at", "-id")

        task_status = request.query_params.get("status")

        valid_statuses = {choice[0] for choice in TaskClaim.Status.choices}

        if task_status:
            if task_status not in valid_statuses:
                return Response(
                    {
                        "detail": "Invalid task status.",
                        "valid_statuses": sorted(valid_statuses),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

            tasks = tasks.filter(status=task_status)

        paginator = AdminUserPagination()
        page = paginator.paginate_queryset(
            tasks,
            request,
            view=self,
        )

        results = [
            {
                "id": task.id,
                "task_id": task.task_id,
                "order_id": task.task.order_id,
                "service_name": task.task.order.service.name if task.task.order_id else None,
                "task_name": task.task.display_name,
                "unit_number": task.task.unit_number,
                "target_link": task.task.target_link,
                "status": task.status,
                "reward_amount": format(task.task.reward_amount, ".8f"),
                "platform_username": user.platform_username,
                "submission_note": task.submission_note,
                "claimed_at": (
                    task.claimed_at.isoformat() if task.claimed_at else None
                ),
                "submitted_at": (
                    task.submitted_at.isoformat() if task.submitted_at else None
                ),
                "reviewed_at": (
                    task.reviewed_at.isoformat() if task.reviewed_at else None
                ),
                "review_note": task.review_note,
            }
            for task in page
        ]

        return paginator.get_paginated_response(results)


class AdminUserLevelsAPIView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        levels = UserLevel.objects.all().order_by("minimum_approved_tasks", "display_order")
        return Response(AdminUserLevelSerializer(levels, many=True).data)

    def post(self, request):
        serializer = AdminUserLevelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        level = serializer.save()
        return Response(AdminUserLevelSerializer(level).data, status=status.HTTP_201_CREATED)


class AdminUserLevelDetailAPIView(APIView):
    permission_classes = [IsAdminUser]

    def patch(self, request, level_id):
        level = get_object_or_404(UserLevel, pk=level_id)
        serializer = AdminUserLevelSerializer(level, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)


class AdminDuplicateRegistrationsAPIView(APIView):
    permission_classes = [IsAdminUser]

    class Pagination(PageNumberPagination):
        page_size = 20
        page_size_query_param = "page_size"
        max_page_size = 100

    def get(self, request):
        flags = DuplicateRegistrationFlag.objects.select_related(
            "new_user", "matched_user", "reviewed_by"
        ).order_by("-created_at", "-id")
        review_status = request.query_params.get("status", "")
        match_type = request.query_params.get("match_type", "")
        valid_statuses = set(DuplicateRegistrationFlag.ReviewStatus.values)
        valid_match_types = set(DuplicateRegistrationFlag.MatchType.values)
        if review_status and review_status not in valid_statuses:
            raise ValidationError({"status": "Invalid duplicate review status."})
        if match_type and match_type not in valid_match_types:
            raise ValidationError({"match_type": "Invalid duplicate match type."})
        if review_status:
            flags = flags.filter(status=review_status)
        if match_type:
            flags = flags.filter(match_type=match_type)
        search = request.query_params.get("search", "").strip()
        if search:
            flags = flags.filter(
                Q(new_user__email__icontains=search)
                | Q(matched_user__email__icontains=search)
                | Q(new_user__full_name__icontains=search)
                | Q(matched_user__full_name__icontains=search)
            )
        paginator = self.Pagination()
        page = paginator.paginate_queryset(flags, request, view=self)
        results = [{
            "id": flag.id,
            "new_user": {"id": flag.new_user_id, "full_name": flag.new_user.full_name, "email": flag.new_user.email},
            "matched_user": {"id": flag.matched_user_id, "full_name": flag.matched_user.full_name, "email": flag.matched_user.email},
            "match_type": flag.match_type,
            "status": flag.status,
            "created_at": flag.created_at.isoformat(),
            "reviewed_at": flag.reviewed_at.isoformat() if flag.reviewed_at else None,
            "reviewed_by": flag.reviewed_by.email if flag.reviewed_by else None,
            "admin_notes": flag.admin_notes,
        } for flag in page]
        return paginator.get_paginated_response(results)
