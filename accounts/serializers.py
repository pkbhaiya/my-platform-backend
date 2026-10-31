from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email as django_validate_email
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import serializers

from .identity import USERNAME_TAKEN_MESSAGE, normalize_username, username_taken
from referrals.services import INVITE_CODE_INVALID, INVITE_CODE_REQUIRED, check_invite_code, normalize_invite_code

from .constants import (
    GENDER_LOCKED_MESSAGE, INVALID_GENDER_MESSAGE, INVALID_STATE_MESSAGE, STATE_CHOICES, STATE_LOCKED_MESSAGE,
)
from .models import DuplicateRegistrationFlag, User, UserChangeLog, UserLevel


def state_field(required=True):
    """State / Union Territory code from accounts.constants; anything else is refused."""
    return serializers.ChoiceField(
        choices=STATE_CHOICES, required=required,
        error_messages={key: INVALID_STATE_MESSAGE for key in ("required", "null", "blank", "invalid_choice")},
    )


def gender_field(required=True):
    """Male or Female; anything else (including the older values) is refused."""
    return serializers.ChoiceField(
        choices=User.Gender.choices, required=required,
        error_messages={key: INVALID_GENDER_MESSAGE for key in ("required", "null", "blank", "invalid_choice")},
    )


class ProfileUpdateSerializer(serializers.ModelSerializer):
    """My Profile. State and gender are chosen at sign-up and users never change
    them; an older account created without a state may set it once (then it is
    locked too). Only an admin can change the gender."""

    state = state_field(required=False)
    gender = serializers.CharField(required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ("state", "gender")

    def validate_gender(self, value):
        if value != getattr(self.instance, "gender", None):
            raise serializers.ValidationError(GENDER_LOCKED_MESSAGE)
        return value

    def validate_state(self, value):
        current = getattr(self.instance, "state", None)
        if current and value != current:
            raise serializers.ValidationError(STATE_LOCKED_MESSAGE)
        return value

    def update(self, instance, validated_data):
        state = validated_data.get("state")
        if not state or state == instance.state:
            return instance
        with transaction.atomic():
            # Conditional: of two simultaneous first-time saves, only one wins.
            if not User.objects.filter(Q(state__isnull=True) | Q(state=""), pk=instance.pk).update(state=state):
                raise serializers.ValidationError({"state": [STATE_LOCKED_MESSAGE]})
            UserChangeLog.objects.create(user=instance, field="state", old_value="", new_value=state, changed_by=instance)
        instance.state = state
        return instance


class AdminUserCorrectionSerializer(serializers.Serializer):
    """An admin correcting a user's state and/or gender (users cannot change them)."""

    state = state_field(required=False)
    gender = gender_field(required=False)

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError("Send a state or a gender to change.")
        return attrs


class SignupSerializer(serializers.ModelSerializer):
    """Validate registration data and create a pending user account."""

    password = serializers.CharField(
        write_only=True,
        trim_whitespace=False,
        style={"input_type": "password"},
    )

    confirm_password = serializers.CharField(
        write_only=True,
        trim_whitespace=False,
        style={"input_type": "password"},
    )

    # Optional invite code (a user's referral code). It is checked before the
    # account exists: a wrong or inactive code is reported, never dropped silently.
    invite_code = serializers.CharField(
        write_only=True, required=False, allow_blank=True, allow_null=True, max_length=32,
        # Anything this long cannot be a code: the same message as any unknown code.
        error_messages={"max_length": INVITE_CODE_INVALID},
    )
    state = state_field()
    gender = gender_field()

    class Meta:
        model = User

        fields = (
            "id",
            "full_name",
            "email",
            "platform_username",
            "whatsapp_number",
            "state",
            "gender",
            "password",
            "confirm_password",
            "invite_code",
            "approval_status",
        )

        read_only_fields = ("id", "approval_status")

    def validate_email(self, value):
        email = value.strip().lower()

        try:
            django_validate_email(email)
        except DjangoValidationError:
            raise serializers.ValidationError(
                "Enter a valid email address."
            )

        if User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(
                "An account with this email already exists."
            )

        return email

    def validate_platform_username(self, value):
        key = normalize_username(value)
        if not key:
            raise serializers.ValidationError("Enter your platform username.")
        if username_taken(key):
            raise serializers.ValidationError(USERNAME_TAKEN_MESSAGE)
        return value

    def validate_password(self, value):
        # An unsaved user built from the submitted details lets the similarity
        # validator reject passwords close to the email, name or username.
        # Non-text values are skipped by the validator itself.
        candidate = User(**{
            field: self.initial_data.get(field)
            for field in ("email", "full_name", "platform_username")
        })
        try:
            validate_password(value, user=candidate)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(exc.messages)

        return value

    def validate_invite_code(self, value):
        _referrer, error = check_invite_code(value)
        if error:
            raise serializers.ValidationError(error)
        return normalize_invite_code(value)

    def validate(self, attrs):
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({
                "confirm_password": "Passwords do not match."
            })
        # Registration closed but open to invited people (SignupAPIView sets this).
        if self.context.get("invite_only") and not attrs.get("invite_code"):
            raise serializers.ValidationError({"invite_code": INVITE_CODE_REQUIRED})

        return attrs

    def create(self, validated_data):
        try:
            return self._create(validated_data)
        except IntegrityError:
            # Two sign-ups with the same email or username raced past validation.
            if User.objects.filter(email__iexact=validated_data["email"]).exists():
                raise serializers.ValidationError({"email": "An account with this email already exists."})
            raise serializers.ValidationError({"platform_username": USERNAME_TAKEN_MESSAGE})

    @transaction.atomic
    def _create(self, validated_data):
        validated_data.pop("confirm_password")
        validated_data.pop("invite_code", None)

        # Normalize the submitted values.
        validated_data["platform_username"] = (
            validated_data["platform_username"].strip()
        )
        validated_data["whatsapp_number"] = (
            validated_data["whatsapp_number"].strip()
        )

        # Find possible matches before creating the new user.
        username_match_ids = list(
            User.objects.filter(
                platform_username__iexact=validated_data["platform_username"]
            ).values_list("id", flat=True)
        )

        whatsapp_match_ids = list(
            User.objects.filter(
                whatsapp_number__iexact=validated_data["whatsapp_number"]
            ).values_list("id", flat=True)
        )

        # Create the new user with pending approval.
        user = User.objects.create_user(
            **validated_data,
            approval_status=User.ApprovalStatus.PENDING,
        )

        flags = []

        for matched_user_id in username_match_ids:
            flags.append(
                DuplicateRegistrationFlag(
                    new_user=user,
                    matched_user_id=matched_user_id,
                    match_type=(
                        DuplicateRegistrationFlag.MatchType.PLATFORM_USERNAME
                    ),
                )
            )

        for matched_user_id in whatsapp_match_ids:
            flags.append(
                DuplicateRegistrationFlag(
                    new_user=user,
                    matched_user_id=matched_user_id,
                    match_type=(
                        DuplicateRegistrationFlag.MatchType.WHATSAPP_NUMBER
                    ),
                )
            )

        if flags:
            DuplicateRegistrationFlag.objects.bulk_create(
                flags,
                ignore_conflicts=True,
            )

        return user

class CurrentUserSerializer(serializers.ModelSerializer):
    """Return information about the currently logged-in user."""

    strikes = serializers.SerializerMethodField()
    state_display = serializers.CharField(source="get_state_display", read_only=True, default=None)
    gender_display = serializers.CharField(source="gender_label", read_only=True, default=None)

    def get_strikes(self, obj):
        from .strike_policy import strike_summary
        summary = strike_summary(obj)
        return {key: summary[key] for key in ("strike_count", "strike_limit", "strike_limit_reached", "current_level")}

    class Meta:
        model = User
        fields = (
            "id",
            "full_name",
            "email",
            "platform_username",
            "whatsapp_number",
            "state",
            "state_display",
            "gender",
            "gender_display",
            "approval_status",
            "status_reason",
            "status_changed_at",
            "strikes",
            "date_joined",
            # Lets the admin panel show superuser-only pages; the API enforces them itself.
            "is_staff",
            "is_superuser",
        )
        read_only_fields = fields



class AdminUserSerializer(serializers.ModelSerializer):
    """Read-only user information for the admin panel.

    Strike data uses ``approved_count``/``strike_total`` annotations and the
    ``levels``/``all_levels`` context supplied by the list view when present.
    """

    strikes = serializers.SerializerMethodField()
    # Display only; supplied by the admin user list for the page being shown.
    target_not_available_rate = serializers.SerializerMethodField()
    linked_accounts = serializers.SerializerMethodField()
    referred_by = serializers.SerializerMethodField()
    referrals_count = serializers.SerializerMethodField()
    state_display = serializers.CharField(source="get_state_display", read_only=True, default=None)
    gender_display = serializers.CharField(source="gender_label", read_only=True, default=None)

    def get_referred_by(self, obj):
        from .admin_views import referred_by

        return referred_by(obj)

    def get_referrals_count(self, obj):
        count = getattr(obj, "referrals_count", None)
        return obj.referrals_made.count() if count is None else count

    def get_strikes(self, obj):
        from .strike_policy import strike_summary
        return strike_summary(
            obj,
            levels=self.context.get("levels"),
            all_levels=self.context.get("all_levels"),
            strikes=getattr(obj, "strike_total", None),
            approved=getattr(obj, "approved_count", None),
        )

    def get_target_not_available_rate(self, obj):
        return self.context.get("target_not_available_rates", {}).get(obj.pk)

    def get_linked_accounts(self, obj):
        """How many other accounts share an IP address or device with this one."""
        return len(self.context.get("linked_accounts", {}).get(obj.pk, {}))

    class Meta:
        model = User
        fields = (
            "id",
            "full_name",
            "email",
            "platform_username",
            "whatsapp_number",
            "state",
            "state_display",
            "gender",
            "gender_display",
            "approval_status",
            "status_reason",
            "status_change_source",
            "status_changed_at",
            "strikes",
            "target_not_available_rate",
            "linked_accounts",
            "referral_code",
            "referred_by",
            "referrals_count",
            "is_active",
            "date_joined",
        )
        read_only_fields = fields


class AdminUserListSerializer(serializers.ModelSerializer):
    """One row of the admin Users list: only the columns the table shows.

    Needs the ``approved_count`` and ``strike_total`` annotations and the
    ``levels`` (active, by threshold) and ``all_levels`` context the list view
    supplies, so a page costs no query per row. Everything else is in the
    user detail view.
    """

    username = serializers.CharField(source="platform_username", read_only=True)
    approval_status_display = serializers.CharField(source="get_approval_status_display", read_only=True)
    level = serializers.SerializerMethodField()
    strikes = serializers.SerializerMethodField()
    joined_on = serializers.SerializerMethodField()

    def _summary(self, obj):
        from .strike_policy import strike_summary

        return strike_summary(obj, levels=self.context["levels"], all_levels=self.context["all_levels"],
                              strikes=obj.strike_total, approved=obj.approved_count)

    def get_level(self, obj):
        return self._summary(obj)["current_level"]

    def get_strikes(self, obj):
        summary = self._summary(obj)
        return {"count": summary["strike_count"], "limit": summary["strike_limit"], "reached": summary["strike_limit_reached"]}

    def get_joined_on(self, obj):
        # The local (IST) calendar day; the full timestamp is in the detail view.
        return timezone.localdate(obj.date_joined).isoformat()

    class Meta:
        model = User
        fields = ("id", "username", "level", "approval_status", "approval_status_display", "strikes", "joined_on",
                  "is_staff", "is_superuser", "is_active")
        read_only_fields = fields


class AdminSetPasswordSerializer(serializers.Serializer):
    # Never trimmed: a space can be part of a password. Checked against AUTH_PASSWORD_VALIDATORS by the view.
    new_password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)
    confirm_password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)

    def validate(self, attrs):
        if attrs["new_password"] != attrs["confirm_password"]:
            raise serializers.ValidationError({"confirm_password": ["The two passwords do not match."]})
        return attrs


class AdminUserStatusSerializer(serializers.ModelSerializer):
    """Validate admin changes to a user's approval status."""

    # Declared explicitly so it is required: the model field has a default,
    # which would otherwise make it optional.
    approval_status = serializers.ChoiceField(choices=User.ApprovalStatus.choices)
    # Optional explanation shown to the user on their Account Status page.
    reason = serializers.CharField(required=False, allow_blank=True, max_length=255, trim_whitespace=True)

    class Meta:
        model = User
        fields = ("approval_status", "reason")

    def validate_approval_status(self, value):
        allowed_statuses = {
            User.ApprovalStatus.APPROVED,
            User.ApprovalStatus.DISAPPROVED,
            User.ApprovalStatus.DISABLED,
        }

        if value not in allowed_statuses:
            raise serializers.ValidationError(
                "Status must be approved, disapproved, or disabled."
            )

        return value


class AdminUserLevelSerializer(serializers.ModelSerializer):
    code = serializers.SlugField(max_length=30)
    badge_color = serializers.RegexField(r"^#[0-9A-Fa-f]{6}$", max_length=20)
    strike_limit = serializers.IntegerField(
        min_value=1, max_value=1000, required=False,
        error_messages={
            "min_value": "Strike limit must be at least 1.",
            "max_value": "Strike limit cannot exceed 1000.",
            "invalid": "Strike limit must be a whole number.",
        },
    )

    class Meta:
        model = UserLevel
        fields = (
            "id", "code", "name", "minimum_approved_tasks", "display_order",
            "badge_color", "description", "benefits", "extra_earning_description",
            "demo_video_url", "strike_limit", "is_active", "created_at", "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_code(self, value):
        if self.instance and value != self.instance.code:
            raise serializers.ValidationError("A level code cannot be changed after creation because services reference it.")
        return value

    def validate(self, attrs):
        if self.instance and attrs.get("is_active") is False:
            from services_catalog.models import Service
            if Service.objects.filter(is_active=True, eligible_level=self.instance.code).exists():
                raise serializers.ValidationError({"is_active": "Disable or reassign active services before disabling this level."})
        return attrs
