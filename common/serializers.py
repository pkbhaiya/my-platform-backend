import re
from datetime import datetime
from decimal import Decimal

from django.utils import timezone
from rest_framework import serializers

from .maintenance import set_maintenance
from .models import (
    CLAIM_TIMEOUT_MAX_MINUTES,
    CLAIM_TIMEOUT_MIN_MINUTES,
    ORDER_MAX_AGE_MAX_HOURS,
    ORDER_MAX_AGE_MIN_HOURS,
    REFERRAL_MONTHLY_CAP_MAX,
    REFERRAL_REQUIRED_TASKS_MAX,
    REFERRAL_WINDOW_MAX_DAYS,
    TARGET_WAIT_MAX_SECONDS,
    TARGET_WAIT_MIN_SECONDS,
    TNA_PAUSE_MAX_REPORTS,
    WITHDRAWAL_HOLD_MAX_HOURS,
    SettingsChangeLog,
    SiteConfiguration,
)

TARGET_WAIT_RANGE_MESSAGE = (
    f"Enter a whole number of seconds from {TARGET_WAIT_MIN_SECONDS} to {TARGET_WAIT_MAX_SECONDS}."
)


def money_field(minimum, max_digits=12):
    message = f"Enter an amount of at least {minimum} with at most 2 decimal places."
    return serializers.DecimalField(
        required=False, max_digits=max_digits, decimal_places=2, min_value=Decimal(str(minimum)),
        error_messages={key: message for key in ("invalid", "min_value", "max_digits", "max_decimal_places", "max_whole_digits", "null")},
    )


def log_value(value):
    """How a setting value is written to SettingsChangeLog."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def whole_number_field(minimum, maximum, unit):
    message = f"Enter a whole number of {unit} from {minimum} to {maximum}."
    return serializers.IntegerField(
        required=False, min_value=minimum, max_value=maximum,
        error_messages={key: message for key in ("invalid", "min_value", "max_value", "null")},
    )


TELEGRAM_URL_PATTERN = re.compile(r"https://(t|telegram)\.me/[^\s/?#][^\s]*")
TELEGRAM_URL_MESSAGE = "Enter a valid Telegram link, e.g. https://t.me/yourchannel"


def telegram_url(value):
    """The configured Telegram link if it is a t.me / telegram.me link, else None (never a guessed link)."""
    value = (value or "").strip()
    return value if TELEGRAM_URL_PATTERN.fullmatch(value) else None


def whatsapp_url(contact):
    """Build a wa.me link only from a configured international number (+country code).

    Returns None for blank or ambiguous values so no link is ever guessed.
    """
    contact = (contact or "").strip()
    if not contact.startswith("+"):
        return None
    if re.search(r"[^0-9+\s().-]", contact) or contact.count("+") != 1:
        return None
    digits = re.sub(r"\D", "", contact)
    if not 8 <= len(digits) <= 15 or digits.startswith("0"):
        return None
    return f"https://wa.me/{digits}"


class ApprovalInstructionsSerializer(serializers.ModelSerializer):
    """Expose only the public account-approval and support information."""

    whatsapp_url = serializers.SerializerMethodField()

    def get_whatsapp_url(self, obj):
        return whatsapp_url(obj.whatsapp_contact)

    class Meta:
        model = SiteConfiguration
        fields = (
            "company_profile_image_url",
            "company_username",
            "whatsapp_contact",
            "whatsapp_url",
            "approval_title",
            "approval_instructions",
            "approval_instructions_hi",
            "disabled_account_message",
        )

        read_only_fields = fields

class AdminSiteConfigurationSerializer(serializers.ModelSerializer):
    """Every admin-editable setting, validated; PATCH one tab's fields at a time.

    Each saved change is written to SettingsChangeLog (who, field, old -> new).
    The maintenance switch goes through set_maintenance, which also pauses
    claim deadlines, records the window and alerts the admins.
    """

    whatsapp_url = serializers.SerializerMethodField()
    target_wait_seconds = serializers.IntegerField(
        required=False,
        min_value=TARGET_WAIT_MIN_SECONDS,
        max_value=TARGET_WAIT_MAX_SECONDS,
        error_messages={key: TARGET_WAIT_RANGE_MESSAGE for key in ("invalid", "min_value", "max_value", "null")},
    )
    claim_timeout_minutes = whole_number_field(CLAIM_TIMEOUT_MIN_MINUTES, CLAIM_TIMEOUT_MAX_MINUTES, "minutes")
    tna_pause_min_reports = whole_number_field(1, TNA_PAUSE_MAX_REPORTS, "reports")
    tna_pause_percent = whole_number_field(1, 100, "percent")
    order_max_age_hours = whole_number_field(ORDER_MAX_AGE_MIN_HOURS, ORDER_MAX_AGE_MAX_HOURS, "hours")
    referral_required_approved_tasks = whole_number_field(1, REFERRAL_REQUIRED_TASKS_MAX, "approved tasks")
    referral_qualify_within_days = whole_number_field(0, REFERRAL_WINDOW_MAX_DAYS, "days (0 for no limit)")
    referral_monthly_bonus_cap = whole_number_field(0, REFERRAL_MONTHLY_CAP_MAX, "bonuses (0 for unlimited)")
    referral_bonus_amount = money_field("0.01")
    min_withdrawal_amount = money_field("0.01")
    withdrawal_max_per_day = money_field(0)
    first_withdrawal_hold_hours = whole_number_field(0, WITHDRAWAL_HOLD_MAX_HOURS, "hours (0 for no hold)")
    maintenance_enabled = serializers.BooleanField(required=False)

    def get_whatsapp_url(self, obj):
        return whatsapp_url(obj.whatsapp_contact)

    def validate(self, attrs):
        current = lambda name: attrs.get(name, getattr(self.instance, name, None))  # noqa: E731
        # A claim must outlive the Open Target wait, or no claim could ever be submitted.
        timeout, wait = current("claim_timeout_minutes"), current("target_wait_seconds")
        if timeout is not None and wait is not None and timeout * 60 <= wait:
            raise serializers.ValidationError({
                "claim_timeout_minutes": "The claim timeout must be longer than the target wait duration.",
            })
        # A daily maximum below the minimum would block every withdrawal.
        daily, minimum = current("withdrawal_max_per_day"), current("min_withdrawal_amount")
        if daily and minimum and daily < minimum:
            raise serializers.ValidationError({
                "withdrawal_max_per_day": f"The daily maximum must be 0 (no limit) or at least the minimum withdrawal (₹{minimum}).",
            })
        return attrs

    def validate_company_profile_image_url(self, value):
        value = (value or "").strip()
        if value and not value.lower().startswith(("https://", "http://")):
            raise serializers.ValidationError("Enter an image address starting with https://.")
        return value

    def validate_company_username(self, value):
        return (value or "").strip()

    def validate_approval_title(self, value):
        if not (value or "").strip():
            raise serializers.ValidationError("Enter the title shown to accounts waiting for approval.")
        return value.strip()

    def validate_approval_instructions(self, value):
        return (value or "").strip()

    def validate_approval_instructions_hi(self, value):
        return (value or "").strip()

    def validate_telegram_url(self, value):
        value = (value or "").strip()
        if value and telegram_url(value) is None:
            raise serializers.ValidationError(TELEGRAM_URL_MESSAGE)
        return value

    def validate_whatsapp_contact(self, value):
        value = (value or "").strip()
        if value and whatsapp_url(value) is None:
            raise serializers.ValidationError(
                "Enter the full international WhatsApp number starting with + and the country code, for example +91 98765 43210."
            )
        return value

    def validate_registration_closed_message(self, value):
        if not (value or "").strip():
            raise serializers.ValidationError("Enter the message shown while registration is closed.")
        return value.strip()

    def validate_maintenance_message(self, value):
        if not (value or "").strip():
            raise serializers.ValidationError("Enter the message shown on the maintenance screen.")
        return value.strip()

    def validate_maintenance_expected_end(self, value):
        unchanged = self.instance is not None and value == self.instance.maintenance_expected_end
        if value is not None and not unchanged and value <= timezone.now():
            raise serializers.ValidationError("Choose a time in the future, or leave it empty.")
        return value

    def update(self, instance, validated_data):
        user = getattr(self.context.get("request"), "user", None)
        by = user if getattr(user, "is_authenticated", False) else None
        enabled = validated_data.pop("maintenance_enabled", None)
        changes = [(name, getattr(instance, name), value) for name, value in validated_data.items() if getattr(instance, name) != value]
        for name, value in validated_data.items():
            setattr(instance, name, value)
        # Save only the submitted fields, so state written meanwhile by other
        # code (the cron heartbeat) is never overwritten.
        instance.save(update_fields=[*validated_data, "updated_at"])
        if enabled is not None and set_maintenance(enabled=enabled, by=by):
            changes.append(("maintenance_enabled", not enabled, enabled))
            instance.refresh_from_db()
        SettingsChangeLog.objects.bulk_create([
            SettingsChangeLog(field=name, old_value=log_value(old), new_value=log_value(new), changed_by=by)
            for name, old, new in changes
        ])
        return instance

    def validate_disabled_account_message(self, value):
        if not (value or "").strip():
            raise serializers.ValidationError("Enter the message shown to disabled accounts.")
        return value.strip()

    class Meta:
        model = SiteConfiguration
        fields = (
            "company_profile_image_url",
            "company_username",
            "whatsapp_contact",
            "whatsapp_url",
            "telegram_url",
            "telegram_button_enabled",
            "approval_title",
            "approval_instructions",
            "approval_instructions_hi",
            "disabled_account_message",
            "min_withdrawal_amount",
            "withdrawal_max_per_day",
            "first_withdrawal_hold_hours",
            "target_wait_seconds",
            "claim_timeout_minutes",
            "tna_pause_min_reports",
            "tna_pause_percent",
            "order_max_age_hours",
            "block_duplicate_active_orders",
            "alert_new_order",
            "alert_task_paused",
            "alert_withdrawal_request",
            "alert_reconciliation_mismatch",
            "alert_order_expired",
            "alert_cron_heartbeat",
            "alert_referral_flagged",
            "alert_referral_summary",
            "referral_enabled",
            "referral_required_approved_tasks",
            "referral_bonus_amount",
            "referral_qualify_within_days",
            "referral_monthly_bonus_cap",
            "registration_open",
            "registration_closed_message",
            "registration_allow_with_referral",
            "maintenance_enabled",
            "maintenance_message",
            "maintenance_expected_end",
            "maintenance_accept_api_orders",
            "maintenance_started_at",
            "alert_maintenance",
            "alert_api_low_balance",
            "alert_api_deposit",
            "alert_api_insufficient_funds",
            "updated_at",
        )
        read_only_fields = ("updated_at", "whatsapp_url", "maintenance_started_at")
        extra_kwargs = {
            "approval_title": {"max_length": 200},
            "approval_instructions": {"max_length": 5000},
            "approval_instructions_hi": {"max_length": 5000},
            "disabled_account_message": {"max_length": 2000},
            "registration_closed_message": {"max_length": 2000},
            "maintenance_message": {"max_length": 2000},
        }
