from rest_framework import exceptions as drf_exceptions
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer
from rest_framework_simplejwt.exceptions import AuthenticationFailed
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.utils import get_md5_hash_password

from .models import User


def account_disabled_error():
    """The admin-configured disabled-account message, as an authentication error."""
    from common.models import SiteConfiguration

    configuration = SiteConfiguration.objects.filter(pk=1).first()
    message = (configuration.disabled_account_message if configuration else "").strip()
    return AuthenticationFailed(message or "Your account has been disabled.", code="account_disabled")


class LoginTokenSerializer(TokenObtainPairSerializer):
    """
    Authenticate users using email and password.

    Pending and Disapproved users may log in so the application can show
    their account status page. Disabled users cannot log in; when their
    password is correct they are told so with the configured message.
    """

    def validate(self, attrs):
        # Emails are stored lower-cased, and the login lookup is an exact match:
        # case-sensitive on SQLite but not on MySQL, so normalize first.
        email = (attrs.get(self.username_field) or "").strip().lower()
        attrs[self.username_field] = email
        try:
            data = super().validate(attrs)
        except drf_exceptions.AuthenticationFailed:
            # Disabled accounts are inactive, so authentication fails before the
            # status check below. Only reveal the reason to someone who knows
            # the correct password; everyone else gets the generic error.
            user = User.objects.filter(email=email).first()
            if (
                user
                and user.approval_status == User.ApprovalStatus.DISABLED
                and user.check_password(attrs.get("password") or "")
            ):
                raise account_disabled_error()
            raise

        user = self.user

        if user.approval_status == User.ApprovalStatus.DISABLED:
            raise account_disabled_error()

        data["user"] = {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
            "approval_status": user.approval_status,
        }

        return data


class ActiveAccountTokenRefreshSerializer(TokenRefreshSerializer):
    """Do not refresh tokens for deactivated or platform-disabled accounts, or after a password change.

    Building the RefreshToken checks the blacklist. SimpleJWT checks the
    password-hash claim (CHECK_REVOKE_TOKEN) only on access tokens, so it is
    checked here too: a refresh token from before the password changed (or
    from before the claim existed) can never mint a new access token.
    """

    def validate(self, attrs):
        token = RefreshToken(attrs["refresh"])
        user_id = token.get("user_id")
        try:
            user = User.objects.get(pk=user_id)
        except User.DoesNotExist:
            raise AuthenticationFailed("The account is no longer available.")
        if not user.is_active or user.approval_status == User.ApprovalStatus.DISABLED:
            raise account_disabled_error()
        if api_settings.CHECK_REVOKE_TOKEN and token.get(api_settings.REVOKE_TOKEN_CLAIM) != get_md5_hash_password(user.password):
            raise AuthenticationFailed("The password of this account was changed. Sign in again.", code="password_changed")
        return super().validate(attrs)
