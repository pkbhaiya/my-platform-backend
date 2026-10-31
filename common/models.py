from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone

TARGET_WAIT_MIN_SECONDS = 1
TARGET_WAIT_MAX_SECONDS = 3600
CLAIM_TIMEOUT_MIN_MINUTES = 5
CLAIM_TIMEOUT_MAX_MINUTES = 1440
TNA_PAUSE_MAX_REPORTS = 1000
ORDER_MAX_AGE_MIN_HOURS = 1
ORDER_MAX_AGE_MAX_HOURS = 2160  # 90 days
REFERRAL_REQUIRED_TASKS_MAX = 1000
REFERRAL_WINDOW_MAX_DAYS = 3650
REFERRAL_MONTHLY_CAP_MAX = 10000
WITHDRAWAL_HOLD_MAX_HOURS = 720  # 30 days


class SiteConfiguration(models.Model):
    """Store editable public-facing site configuration."""

    id = models.PositiveSmallIntegerField(
        primary_key=True,
        default=1,
        editable=False,
    )

    company_profile_image_url = models.URLField(
        max_length=500,
        blank=True,
    )

    company_username = models.CharField(
        max_length=150,
        blank=True,
    )

    whatsapp_contact = models.CharField(
        max_length=20,
        blank=True,
    )

    # The floating "Join us on Telegram" button on the user panel and the
    # sign-in pages; hidden while the link is empty or the switch is off.
    telegram_url = models.CharField(
        max_length=200,
        blank=True,
        help_text="Telegram channel or group link, e.g. https://t.me/yourchannel.",
    )
    telegram_button_enabled = models.BooleanField(default=True, help_text="Show the floating Telegram button.")

    approval_title = models.CharField(
        max_length=200,
        default="Your Account Is Awaiting Approval",
    )

    approval_instructions = models.TextField(
        blank=True,
        default="Please follow the company profile and contact the administrator for account approval.",
    )
    # Optional Hindi version; the Pending Approval page offers a language switch only when it is set.
    approval_instructions_hi = models.TextField(blank=True, default="")

    disabled_account_message = models.TextField(
        default="Your account has been disabled because you did not follow our guidelines.",
    )

    min_withdrawal_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("100.00"),
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="Minimum amount a user can withdraw.",
    )
    # Withdrawal limits (wallet.services); 0 switches each one off.
    withdrawal_max_per_day = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00"), validators=[MinValueValidator(Decimal("0"))],
        help_text="Most one user can request per day (IST), pending and approved requests together; 0 means no limit.",
    )
    first_withdrawal_hold_hours = models.PositiveIntegerField(
        default=0, validators=[MaxValueValidator(WITHDRAWAL_HOLD_MAX_HOURS)],
        help_text="Hours a user's first withdrawal waits before it can be approved; 0 means no hold.",
    )

    # The single source of the Open Target wait. Each claim stores the value in
    # effect when its target is first opened, so a later change applies only to
    # targets opened after it.
    target_wait_seconds = models.PositiveIntegerField(
        default=15,
        validators=[MinValueValidator(TARGET_WAIT_MIN_SECONDS), MaxValueValidator(TARGET_WAIT_MAX_SECONDS)],
        help_text="Seconds a user must wait after opening a Task target before submitting.",
    )

    # A claim still `claimed` this long after claimed_at expires and frees its slot.
    claim_timeout_minutes = models.PositiveIntegerField(
        default=30,
        validators=[MinValueValidator(CLAIM_TIMEOUT_MIN_MINUTES), MaxValueValidator(CLAIM_TIMEOUT_MAX_MINUTES)],
        help_text="Minutes a user may hold a claimed Task before the claim expires and its slot is released.",
    )

    # A Task is paused when its Target Not Available reports reach BOTH limits.
    tna_pause_min_reports = models.PositiveIntegerField(
        default=3,
        validators=[MinValueValidator(1), MaxValueValidator(TNA_PAUSE_MAX_REPORTS)],
        help_text="Target Not Available reports needed before a Task can be paused automatically.",
    )
    tna_pause_percent = models.PositiveIntegerField(
        default=20,
        validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="Share of a Task's submissions (%) that must be Target Not Available to pause it.",
    )

    # Orders older than this that are still open are cancelled by the expiry sweep.
    # Refuse a new order while an earlier order for the same link and service is
    # still open (orders.duplicates). Workers who already claimed that link for
    # that service never see its later Tasks, whatever this says.
    block_duplicate_active_orders = models.BooleanField(
        default=True,
        help_text="Refuse a new order while an order with the same link and service is still open.",
    )

    order_max_age_hours = models.PositiveIntegerField(
        default=72,
        validators=[MinValueValidator(ORDER_MAX_AGE_MIN_HOURS), MaxValueValidator(ORDER_MAX_AGE_MAX_HOURS)],
        help_text="Hours an Order may stay open. Older open Orders are cancelled (Partial when some work was approved).",
    )

    # Telegram alerts (see common.notifications), one switch per event.
    alert_new_order = models.BooleanField(default=True, help_text="Telegram alert for each new order (batched when busy).")
    alert_task_paused = models.BooleanField(default=True, help_text="Telegram alert when a Task is auto-paused for Target Not Available.")
    alert_withdrawal_request = models.BooleanField(default=True, help_text="Telegram alert for each new withdrawal request.")
    alert_reconciliation_mismatch = models.BooleanField(default=True, help_text="Telegram alert when wallet reconciliation finds a mismatch.")
    alert_order_expired = models.BooleanField(default=True, help_text="Telegram alert when an order expires (partial or cancelled).")
    alert_cron_heartbeat = models.BooleanField(default=True, help_text="Telegram alert when expire_claims has not run for 10 minutes.")

    alert_referral_flagged = models.BooleanField(default=True, help_text="Telegram alert when a referral is flagged for review.")
    alert_referral_summary = models.BooleanField(default=True, help_text="Daily Telegram summary of referral bonuses paid.")
    alert_maintenance = models.BooleanField(default=True, help_text="Telegram alert when maintenance mode is turned on or off.")
    alert_api_low_balance = models.BooleanField(default=True, help_text="Telegram alert when an admin wallet's balance falls below its threshold.")
    alert_api_deposit = models.BooleanField(default=True, help_text="Telegram alert when funds are added to an admin wallet.")
    alert_api_insufficient_funds = models.BooleanField(default=True, help_text="Telegram alert when an admin wallet's API orders are refused for insufficient funds (at most every 15 minutes).")

    # Referrals (referrals app). Terms are copied onto each Referral at signup,
    # so later changes never alter a promise already made.
    referral_enabled = models.BooleanField(default=False, help_text="Allow new referrals and show Refer & Earn.")
    referral_required_approved_tasks = models.PositiveIntegerField(
        default=10, validators=[MinValueValidator(1), MaxValueValidator(REFERRAL_REQUIRED_TASKS_MAX)],
        help_text="Approved Tasks the referred user needs before the referrer is paid.",
    )
    referral_bonus_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("10.00"), validators=[MinValueValidator(Decimal("0.01"))],
        help_text="One-time bonus (INR) per qualifying referred user.",
    )
    referral_qualify_within_days = models.PositiveIntegerField(
        default=60, validators=[MaxValueValidator(REFERRAL_WINDOW_MAX_DAYS)],
        help_text="Days after signup to qualify; 0 means no limit.",
    )
    referral_monthly_bonus_cap = models.PositiveIntegerField(
        default=0, validators=[MaxValueValidator(REFERRAL_MONTHLY_CAP_MAX)],
        help_text="Most bonuses one referrer can receive per calendar month (IST); 0 means unlimited.",
    )

    # Registration control. Closing it is enforced by the signup API itself.
    registration_open = models.BooleanField(default=True, help_text="Allow new sign-ups.")
    registration_closed_message = models.TextField(
        default="Registrations are currently closed. Please check back later.",
        help_text="Shown on the sign-up page while registration is closed.",
    )
    registration_allow_with_referral = models.BooleanField(
        default=False, help_text="While closed, still accept sign-ups that use a valid referral code (invite-only).",
    )

    # Maintenance mode (common.maintenance). The switch itself changes only
    # through set_maintenance, which records who changed it and when.
    maintenance_enabled = models.BooleanField(default=False, editable=False, help_text="Show the maintenance screen to users.")
    maintenance_message = models.TextField(
        default="We are performing scheduled maintenance. Please check back soon.",
        help_text="Shown on the maintenance screen.",
    )
    maintenance_expected_end = models.DateTimeField(null=True, blank=True, help_text="Optional; shown as \"Expected back at ...\".")
    maintenance_accept_api_orders = models.BooleanField(
        default=True, help_text="Keep accepting /api/v2 orders during maintenance (their Tasks wait).",
    )
    # When the current maintenance began; claim deadlines are paused from here.
    maintenance_started_at = models.DateTimeField(null=True, blank=True, editable=False)

    # Runtime state for the cron heartbeat; not edited by admins.
    expire_claims_last_run_at = models.DateTimeField(null=True, blank=True, editable=False)
    cron_alert_sent_at = models.DateTimeField(null=True, blank=True, editable=False)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Site Configuration"
        verbose_name_plural = "Site Configuration"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(min_withdrawal_amount__gt=0),
                name="site_min_withdrawal_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(target_wait_seconds__gte=TARGET_WAIT_MIN_SECONDS, target_wait_seconds__lte=TARGET_WAIT_MAX_SECONDS),
                name="site_target_wait_seconds_range",
            ),
            models.CheckConstraint(
                condition=models.Q(claim_timeout_minutes__gte=CLAIM_TIMEOUT_MIN_MINUTES, claim_timeout_minutes__lte=CLAIM_TIMEOUT_MAX_MINUTES),
                name="site_claim_timeout_minutes_range",
            ),
            models.CheckConstraint(
                condition=models.Q(tna_pause_min_reports__gte=1, tna_pause_min_reports__lte=TNA_PAUSE_MAX_REPORTS),
                name="site_tna_pause_min_reports_range",
            ),
            models.CheckConstraint(
                condition=models.Q(tna_pause_percent__gte=1, tna_pause_percent__lte=100),
                name="site_tna_pause_percent_range",
            ),
            models.CheckConstraint(
                condition=models.Q(order_max_age_hours__gte=ORDER_MAX_AGE_MIN_HOURS, order_max_age_hours__lte=ORDER_MAX_AGE_MAX_HOURS),
                name="site_order_max_age_hours_range",
            ),
            models.CheckConstraint(
                condition=models.Q(referral_required_approved_tasks__gte=1, referral_required_approved_tasks__lte=REFERRAL_REQUIRED_TASKS_MAX),
                name="site_referral_required_tasks_range",
            ),
            models.CheckConstraint(
                condition=models.Q(referral_bonus_amount__gte=Decimal("0.01")),
                name="site_referral_bonus_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(referral_qualify_within_days__lte=REFERRAL_WINDOW_MAX_DAYS),
                name="site_referral_window_range",
            ),
            models.CheckConstraint(
                condition=models.Q(referral_monthly_bonus_cap__lte=REFERRAL_MONTHLY_CAP_MAX),
                name="site_referral_cap_range",
            ),
            models.CheckConstraint(
                condition=models.Q(withdrawal_max_per_day__gte=0),
                name="site_withdrawal_max_per_day_nonnegative",
            ),
            models.CheckConstraint(
                condition=models.Q(first_withdrawal_hold_hours__lte=WITHDRAWAL_HOLD_MAX_HOURS),
                name="site_first_withdrawal_hold_range",
            ),
        ]

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        """The singleton row, created with defaults on first use."""
        configuration, _ = cls.objects.get_or_create(pk=1)
        return configuration

    def __str__(self):
        return "Site Configuration"


class OrderAlertWindow(models.Model):
    """New orders per 5-minute window, so busy windows get one summary alert instead of many."""

    window_start = models.DateTimeField(unique=True)
    orders = models.PositiveIntegerField(default=0)
    summary_sent_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.orders} order(s) from {self.window_start:%Y-%m-%d %H:%M}"


class MaintenanceEvent(models.Model):
    """One row each time maintenance mode is turned on or off."""

    enabled = models.BooleanField()
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField()
    # On "off" rows: how long maintenance lasted, and how many active claims
    # had their deadline moved by that much.
    duration_seconds = models.PositiveIntegerField(null=True, blank=True)
    claims_extended = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"Maintenance {'on' if self.enabled else 'off'} at {self.created_at:%Y-%m-%d %H:%M}"


class SettingsChangeLog(models.Model):
    """One row per Site Configuration field an admin changed: who, when, old -> new."""

    field = models.CharField(max_length=64, db_index=True)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    changed_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-changed_at", "-id"]

    def __str__(self):
        return f"{self.field} changed at {self.changed_at:%Y-%m-%d %H:%M}"
