import logging

from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.generics import CreateAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import InvalidToken, TokenError
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from common.maintenance import maintenance_response
from common.models import SiteConfiguration
from common.serializers import ApprovalInstructionsSerializer
from tasks.models import TaskStrike

from .auth_serializers import ActiveAccountTokenRefreshSerializer, LoginTokenSerializer
from referrals.services import INVITE_CODE_INACTIVE, create_signup_referral

from .identity import record_request_signals
from .models import AccountSignal, User
from .serializers import CurrentUserSerializer, ProfileUpdateSerializer, SignupSerializer
from .strike_policy import active_levels, approved_claim_count, level_for_count, strike_summary

logger = logging.getLogger(__name__)


class SignupAPIView(CreateAPIView):
    """
    Public API endpoint for user registration.
    New accounts remain pending until admin approval.
    """

    serializer_class = SignupSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "signup"
    # True while registration is closed but open to people with a valid invite code.
    invite_only = False

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "invite_only": self.invite_only}

    def create(self, request, *args, **kwargs):
        # Enforced here, not only hidden in the frontend.
        config = SiteConfiguration.load()
        if config.maintenance_enabled:
            return maintenance_response(config)
        if not config.registration_open and not config.registration_allow_with_referral:
            return Response(
                {"detail": config.registration_closed_message, "code": "registration_closed"},
                status=status.HTTP_403_FORBIDDEN,
            )
        self.invite_only = not config.registration_open
        return super().create(request, *args, **kwargs)

    @transaction.atomic
    def perform_create(self, serializer):
        # One transaction: an invite code that stopped working between validation
        # and now rolls the new account back, so no account is created with it.
        user = serializer.save()
        record_request_signals(user, self.request, AccountSignal.SignalType.SIGNUP_IP)
        # After the sign-up signals, so the referral's abuse check can see them.
        code = serializer.validated_data.get("invite_code")
        if not code:
            return
        try:
            with transaction.atomic():
                referral = create_signup_referral(referred=user, code=code)
        except Exception:  # noqa: BLE001 - an unexpected referral error must never fail the signup
            logger.exception("Referral could not be created for user #%s", user.pk)
            return
        if referral is None:
            # The owner was disabled, or referrals were switched off, after validation.
            raise ValidationError({"invite_code": [INVITE_CODE_INACTIVE]})


class LoginAPIView(TokenObtainPairView):
    """
    Authenticate users with email and password.
    Returns JWT access and refresh tokens.
    """

    serializer_class = LoginTokenSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "login"

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            raise InvalidToken(exc.args[0])
        record_request_signals(serializer.user, request, AccountSignal.SignalType.LOGIN_IP)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class RefreshAPIView(TokenRefreshView):
    """Exchange a valid refresh token for a new access token."""

    serializer_class = ActiveAccountTokenRefreshSerializer
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "token_refresh"


class CurrentUserAPIView(APIView):
    """GET the signed-in user's profile; PATCH {"state": "<code>"} from My Profile (any account status)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        serializer = CurrentUserSerializer(request.user)
        return Response(serializer.data)

    def patch(self, request):
        serializer = ProfileUpdateSerializer(request.user, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        if serializer.validated_data:
            serializer.save()
        return Response(CurrentUserSerializer(request.user).data)


class MyAccountStatusAPIView(APIView):
    """Everything the User Panel needs to explain the account's current status.

    Status, reason and its recorded source, strike count and the applicable
    level limit, plus the admin-configured support details.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        summary = strike_summary(user)
        return Response({
            "approval_status": user.approval_status,
            "approval_status_display": user.get_approval_status_display(),
            "task_access": user.is_active and user.approval_status == User.ApprovalStatus.APPROVED,
            "status_reason": user.status_reason,
            "status_change_source": user.status_change_source,
            "status_changed_at": user.status_changed_at.isoformat() if user.status_changed_at else None,
            "strikes": {key: summary[key] for key in (
                "strike_count", "strike_limit", "strike_limit_reached", "current_level", "approved_task_count",
            )},
            "support": ApprovalInstructionsSerializer(SiteConfiguration.load()).data,
        })


def serialize_level(level):
    if not level:
        return None
    return {
        "id": level.id, "code": level.code, "name": level.name,
        "minimum_approved_tasks": level.minimum_approved_tasks,
        "badge_color": level.badge_color, "description": level.description,
        "benefits": level.benefits,
        "extra_earning_description": level.extra_earning_description,
        "demo_video_url": level.demo_video_url,
        "strike_limit": level.strike_limit,
    }


class MyLevelAPIView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        approved_count = approved_claim_count(request.user.pk)
        levels = active_levels()
        current = level_for_count(approved_count, levels)
        upcoming = next((level for level in levels if level.minimum_approved_tasks > approved_count), None)
        return Response({
            "approved_task_count": approved_count,
            "strike_count": TaskStrike.objects.filter(user=request.user).count(),
            "current_level": serialize_level(current),
            "next_level": serialize_level(upcoming),
            "tasks_to_next_level": upcoming.minimum_approved_tasks - approved_count if upcoming else 0,
            "levels": [serialize_level(level) for level in levels],
        })
