from django.db.models import Count, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle

from accounts.permissions import CanViewOwnRecords
from common.throttles import PublicEndpointThrottle
from rest_framework.response import Response
from rest_framework.views import APIView

from tasks.models import TaskClaim

from .models import Referral
from .services import (
    INVITE_CODE_APPLIED, check_invite_code, mask_username, referral_link_path, referral_settings, referrer_summary,
)


def approved_count_subquery():
    """Approved claims of each row's referred user, as a correlated subquery."""
    counts = (TaskClaim.objects.filter(user_id=OuterRef("referred_id"), status=TaskClaim.Status.APPROVED)
              .order_by().values("user_id").annotate(total=Count("pk")).values("total"))
    return Coalesce(Subquery(counts[:1]), 0)


class ReferralListPagination(PageNumberPagination):
    page_size = 5
    max_page_size = 5


class MyReferralProgramAPIView(APIView):
    """GET /api/v1/referrals/me/: the user's link, the live rules and their referral stats."""

    # Not for accounts awaiting approval; disapproved accounts can still read.
    permission_classes = [IsAuthenticated, CanViewOwnRecords]

    def get(self, request):
        settings_ = referral_settings()
        if not settings_["referral_enabled"]:
            return Response({"enabled": False})
        return Response({
            "enabled": True,
            "code": request.user.referral_code,
            "link_path": referral_link_path(request.user.referral_code),
            "rules": {
                "required_tasks": settings_["referral_required_approved_tasks"],
                "bonus_amount": format(settings_["referral_bonus_amount"], "f"),
                "qualify_within_days": settings_["referral_qualify_within_days"],
            },
            "stats": referrer_summary(request.user),
        })


class MyReferralsAPIView(APIView):
    """GET /api/v1/referrals/me/list/: the user's referrals, 5 per page, masked usernames only."""

    # Not for accounts awaiting approval; disapproved accounts can still read.
    permission_classes = [IsAuthenticated, CanViewOwnRecords]

    def get(self, request):
        referrals = (Referral.objects.filter(referrer=request.user).select_related("referred")
                     .annotate(approved=approved_count_subquery()).order_by("-created_at", "-id"))
        paginator = ReferralListPagination()
        page = paginator.paginate_queryset(referrals, request, view=self)
        return paginator.get_paginated_response([{
            "id": referral.pk,
            "username": mask_username(referral.referred.platform_username),
            "joined_at": referral.referred.date_joined.isoformat(),
            # Progress stops at the requirement; the extra approvals do not matter here.
            "approved_tasks": min(referral.approved, referral.required_tasks_snapshot),
            "required_tasks": referral.required_tasks_snapshot,
            "status": referral.status,
            "status_display": referral.get_status_display(),
            "bonus_amount": format(referral.bonus_amount_snapshot, "f"),
            "qualify_deadline": referral.qualify_deadline.isoformat() if referral.qualify_deadline else None,
            "paid_at": referral.paid_at.isoformat() if referral.paid_at else None,
        } for referral in page])


class InviteCodeCheckAPIView(APIView):
    """GET /api/v1/public/invite-code/<code>/: {"valid", "message"} for the sign-up form.

    Says nothing about the code's owner. Rate-limited per IP so codes cannot be guessed.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    # Both apply: 20/minute for code guessing, plus the shared public-endpoint limit.
    throttle_classes = [ScopedRateThrottle, PublicEndpointThrottle]
    throttle_scope = "invite_code"

    def get(self, request, code):
        _referrer, error = check_invite_code(code)
        if error:
            return Response({"valid": False, "message": error})
        return Response({"valid": True, "message": INVITE_CODE_APPLIED})
