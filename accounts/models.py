
# Create your models here.
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import IntegrityError, models, transaction
from django.db.models.functions import Length
from django.conf import settings
from django.utils import timezone

from .constants import LEGACY_GENDER_LABELS, STATE_CHOICES
from .identity import USERNAME_TAKEN_MESSAGE, normalize_platform, normalize_username, username_taken
from .referral_codes import REFERRAL_CODE_LENGTH, generate_referral_code

# Marks a platform_username that was deferred when the row was loaded.
_NOT_LOADED = object()
REFERRAL_CODE_ATTEMPTS = 5
from .managers import UserManager


class User(AbstractUser):
    """Custom user model for the platform."""

    # Sign-up offers only these. Older rows may hold "other" or "not_disclosed"
    # (accounts.constants.LEGACY_GENDER_LABELS); they are kept until an admin changes them.
    class Gender(models.TextChoices):
        MALE = "male", "Male"
        FEMALE = "female", "Female"

    class ApprovalStatus(models.TextChoices):
        PENDING = "pending", "Pending Approval"
        APPROVED = "approved", "Approved"
        # Can still sign in and view wallet/withdrawal history, but cannot
        # take part in Tasks. Set automatically at the level's strike limit.
        DISAPPROVED = "disapproved", "Disapproved"
        # Fully locked out: cannot sign in or refresh tokens.
        DISABLED = "disabled", "Disabled"

    # Use email instead of Django's default username for login.
    username = None
    email = models.EmailField(unique=True)

    # Registration details.
    full_name = models.CharField(max_length=150)
    platform_username = models.CharField(max_length=150)
    # One account per platform username: the normalized form (trimmed,
    # lowercased, leading @ removed), set on save. NULL only for later accounts
    # that shared a username before this rule existed (left for admin review).
    platform_username_key = models.CharField(max_length=150, null=True, blank=True, unique=True, editable=False)
    whatsapp_number = models.CharField(max_length=20)
    # Code from accounts.constants; required at sign-up, empty only for older
    # accounts whose free-text state matched no name (they set it on My Profile).
    state = models.CharField(max_length=50, choices=STATE_CHOICES, null=True, blank=True)

    gender = models.CharField(
        max_length=20,
        choices=Gender.choices,
    )

    approval_status = models.CharField(
        max_length=20,
        choices=ApprovalStatus.choices,
        default=ApprovalStatus.PENDING,
    )
    class StatusChangeSource(models.TextChoices):
        NONE = "", "Not recorded"
        STRIKE_LIMIT = "strike_limit", "Strike limit reached"
        ADMIN = "admin", "Administrator"

    # Why the approval status last changed. ``status_reason`` is shown to the
    # user; ``status_change_source`` says whether the strike limit or an admin
    # made the change, so the UI never has to guess from the text.
    status_reason = models.CharField(max_length=255, blank=True, default="")
    status_change_source = models.CharField(
        max_length=20, choices=StatusChangeSource.choices, blank=True, default=StatusChangeSource.NONE,
    )
    status_changed_at = models.DateTimeField(null=True, blank=True)
    status_changed_by = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    # The saved payout UPI ID, normalized (trimmed, lowercased). One account per
    # UPI ID; withdrawals are always paid to it. See wallet.services.set_payout_upi.
    upi_id = models.CharField(max_length=255, null=True, blank=True, unique=True)
    upi_id_changed_at = models.DateTimeField(null=True, blank=True)

    # Shared in the user's referral link (/signup?ref=CODE); set on creation.
    referral_code = models.CharField(max_length=8, unique=True, editable=False)

    objects = UserManager()

    USERNAME_FIELD = "email"

    REQUIRED_FIELDS = [
        "full_name",
        "platform_username",
        "whatsapp_number",
        "state",
        "gender",
    ]

    class Meta(AbstractUser.Meta):
        constraints = [
            # Every code is exactly 8 characters (CHAR_LENGTH on MySQL).
            models.CheckConstraint(
                condition=models.lookups.Exact(Length("referral_code"), REFERRAL_CODE_LENGTH),
                name="user_referral_code_length",
            ),
        ]

    def __str__(self):
        return self.email

    @property
    def gender_label(self):
        """Readable gender, including values from before only Male and Female were offered."""
        return dict(self.Gender.choices).get(self.gender) or LEGACY_GENDER_LABELS.get(self.gender) or self.gender or None

    @property
    def gender_needs_update(self):
        """True for an older value that sign-up no longer offers (an admin can change it)."""
        return bool(self.gender) and self.gender not in self.Gender.values

    @classmethod
    def from_db(cls, db, field_names, values):
        instance = super().from_db(db, field_names, values)
        instance._loaded_platform_username = instance.__dict__.get("platform_username", _NOT_LOADED)
        return instance

    def _platform_username_changed(self):
        loaded = getattr(self, "_loaded_platform_username", _NOT_LOADED)
        return self._state.adding or (loaded is not _NOT_LOADED and loaded != self.platform_username)

    def clean(self):
        super().clean()
        key = normalize_username(self.platform_username)
        if self._platform_username_changed() and username_taken(key, exclude_user_id=self.pk):
            raise ValidationError({"platform_username": USERNAME_TAKEN_MESSAGE})

    def save(self, *args, **kwargs):
        # The key follows the username only when the username changes, so a
        # legacy duplicate left without a key is not given one by other edits.
        if self._platform_username_changed():
            self.platform_username_key = normalize_username(self.platform_username) or None
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "platform_username" in update_fields:
                kwargs["update_fields"] = {*update_fields, "platform_username_key"}
        if self.referral_code:
            super().save(*args, **kwargs)
        else:
            self._save_with_new_referral_code(*args, **kwargs)
        self._loaded_platform_username = self.platform_username

    def _save_with_new_referral_code(self, *args, **kwargs):
        """Every account gets its code here, whichever path created it (sign-up,
        Django admin, createsuperuser). The unique column settles the rare race
        where two new accounts draw the same code: the loser draws again."""
        for attempt in range(REFERRAL_CODE_ATTEMPTS):
            self.referral_code = generate_referral_code(lambda code: User.objects.filter(referral_code=code).exists())
            try:
                with transaction.atomic():
                    super().save(*args, **kwargs)
                return
            except IntegrityError:
                collided = User.objects.filter(referral_code=self.referral_code).exclude(pk=self.pk).exists()
                self.referral_code = ""
                if not collided or attempt == REFERRAL_CODE_ATTEMPTS - 1:
                    raise



class DuplicateRegistrationFlag(models.Model):
    """Record possible duplicate registrations for admin review."""

    class MatchType(models.TextChoices):
        PLATFORM_USERNAME = "platform_username", "Platform Username"
        WHATSAPP_NUMBER = "whatsapp_number", "WhatsApp Number"

    class ReviewStatus(models.TextChoices):
        PENDING = "pending", "Pending Review"
        CONFIRMED = "confirmed", "Confirmed Duplicate"
        FALSE_POSITIVE = "false_positive", "Not a Duplicate"

    new_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="duplicate_flags",
    )

    matched_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="matched_duplicate_flags",
    )

    match_type = models.CharField(
        max_length=30,
        choices=MatchType.choices,
    )

    status = models.CharField(
        max_length=20,
        choices=ReviewStatus.choices,
        default=ReviewStatus.PENDING,
    )

    admin_notes = models.TextField(blank=True)

    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_duplicate_flags",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["new_user", "matched_user", "match_type"],
                name="unique_duplicate_registration_flag",
            ),
        ]

    def __str__(self):
        return (
            f"{self.new_user.email} - "
            f"{self.match_type} - {self.status}"
        )


class PlatformUsername(models.Model):
    """Store additional platform usernames for a user."""

    user = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="additional_platform_usernames",
    )

    platform_name = models.CharField(
        max_length=50,
    )

    username = models.CharField(
        max_length=150,
    )

    # Normalized forms: one account per (platform, username). NULL only for
    # rows that conflicted before this rule existed.
    platform_key = models.CharField(max_length=50, null=True, blank=True, editable=False)
    username_key = models.CharField(max_length=150, null=True, blank=True, editable=False)

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "platform_name", "username"],
                name="unique_user_platform_username",
            ),
            models.UniqueConstraint(
                fields=["platform_key", "username_key"],
                name="unique_platform_username_across_users",
            ),
        ]

    def save(self, *args, **kwargs):
        self.platform_key = normalize_platform(self.platform_name) or None
        self.username_key = normalize_username(self.username) or None
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.platform_name}: {self.username}"


class UserLevel(models.Model):
    """Configurable user levels based on approved task counts."""

    code = models.SlugField(
        max_length=30,
        unique=True,
    )

    name = models.CharField(
        max_length=50,
    )

    minimum_approved_tasks = models.PositiveIntegerField(
        default=0,
        help_text="Minimum number of approved tasks required for this level.",
    )

    display_order = models.PositiveSmallIntegerField(
        unique=True,
        help_text="Controls the order in which levels are displayed.",
    )

    badge_color = models.CharField(
        max_length=20,
        default="#64748B",
    )
    description = models.TextField(blank=True, default="")
    benefits = models.TextField(blank=True, default="")
    extra_earning_description = models.TextField(blank=True, default="")
    demo_video_url = models.URLField(max_length=500, blank=True, default="")
    # Rejection strikes a user at this level may hold before the account is
    # automatically Disapproved (reached when strike count >= this value).
    strike_limit = models.PositiveIntegerField(
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(1000)],
        help_text="Strikes at which an approved user at this level is automatically disapproved.",
    )

    is_active = models.BooleanField(
        default=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["display_order"]
        constraints = [
            models.UniqueConstraint(
                fields=["minimum_approved_tasks"],
                name="unique_level_minimum_approved_tasks",
            ),
            models.CheckConstraint(
                condition=models.Q(strike_limit__gte=1) & models.Q(strike_limit__lte=1000),
                name="userlevel_strike_limit_range",
            ),
        ]

    def __str__(self):
        return self.name


class AccountSignal(models.Model):
    """A hashed sign-up IP, login IP or device ID seen on an account (flag only).

    Accounts sharing a value_hash are shown to admins as linked. Raw values are
    never stored: value_hash is HMAC-SHA256 under ACCOUNT_SIGNAL_SALT.
    """

    class SignalType(models.TextChoices):
        SIGNUP_IP = "signup_ip", "Sign-up IP address"
        LOGIN_IP = "login_ip", "Login IP address"
        DEVICE_ID = "device_id", "Device"

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="account_signals")
    signal_type = models.CharField(max_length=20, choices=SignalType.choices)
    value_hash = models.CharField(max_length=64)
    first_seen = models.DateTimeField()
    last_seen = models.DateTimeField()
    count = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "signal_type", "value_hash"], name="unique_account_signal"),
        ]
        indexes = [
            models.Index(fields=["value_hash"], name="account_signal_hash_idx"),
            models.Index(fields=["signal_type", "last_seen"], name="account_signal_seen_idx"),
        ]

    def __str__(self):
        return f"User #{self.user_id} {self.signal_type}"


class UserChangeLog(models.Model):
    """Who changed a field of a user's record, when, and from what to what (e.g. the state)."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="change_log")
    field = models.CharField(max_length=64)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    changed_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-changed_at", "-id"]
        indexes = [models.Index(fields=["user", "field"], name="user_change_log_field_idx")]

    def __str__(self):
        return f"User #{self.user_id} {self.field} changed at {self.changed_at:%Y-%m-%d %H:%M}"
