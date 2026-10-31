from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models

from services_catalog.models import Service


class Order(models.Model):
    """Store an incoming or manually created service order."""

    class Source(models.TextChoices):
        API = "api", "API"
        MANUAL = "manual", "Manual"

    class AvailabilityStatus(models.TextChoices):
        AVAILABLE = "available", "Available"
        PARTIALLY_CLAIMED = "partially_claimed", "Partially Claimed"
        FULLY_CLAIMED = "fully_claimed", "Fully Claimed"
        UNAVAILABLE = "unavailable", "Unavailable"

    class FulfillmentStatus(models.TextChoices):
        ACCEPTED = "accepted", "Accepted"
        IN_PROGRESS = "in_progress", "In Progress"
        UNDER_REVIEW = "under_review", "Under Review"
        COMPLETED = "completed", "Completed"
        # Task cancelled after some claims were approved: those units were delivered.
        PARTIAL = "partial", "Partially Delivered"
        CANCELLED = "cancelled", "Cancelled"
        REJECTED = "rejected", "Rejected"

    # The same service is reused for future incoming orders.
    service = models.ForeignKey(
        Service,
        on_delete=models.PROTECT,
        related_name="orders",
    )

    source = models.CharField(
        max_length=10,
        choices=Source.choices,
        default=Source.API,
    )

    # Used to identify retries from the external SMM panel; unique per admin
    # wallet (see Meta). Manual orders can leave this field empty.
    external_order_id = models.CharField(
        max_length=100,
        null=True,
        blank=True,
    )

    # The admin wallet whose key placed the order and whose balance pays for it.
    # NULL for manual (admin) orders, which are never charged.
    admin_wallet = models.ForeignKey(
        "orders.AdminWallet",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="orders",
    )
    # INR actually debited from the wallet at creation (the order value
    # ROUND_HALF_UP to 2 dp; see orders.admin_wallets.charge_for). NULL on manual
    # orders and on API orders placed before prepaid balances existed.
    charge_amount = models.DecimalField(
        max_digits=14,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0.01"))],
    )

    # Request key used to safely retry administrator-created manual orders.
    idempotency_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)

    target_link = models.URLField(max_length=2048)
    # The key duplicate orders are matched by (orders.links.canonical_link).
    # Computed from target_link on every save and never edited by hand;
    # target_link itself stays exactly as given, for opening.
    normalized_link = models.CharField(max_length=512, blank=True, default="", editable=False)
    target_type_snapshot = models.CharField(
        max_length=10,
        choices=Service.TargetType.choices,
        default=Service.TargetType.POST,
    )

    quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
    )

    # Keep the applicable financial settings for historical accuracy.
    price_per_1000_snapshot = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0.0001"))],
    )
    fulfillment_cost_per_1000_snapshot = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
    )
    has_fulfillment_cost_snapshot = models.BooleanField(default=False)

    business_share_percentage_snapshot = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0")),
            MaxValueValidator(Decimal("100")),
        ],
    )

    user_reward_percentage_snapshot = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0")),
            MaxValueValidator(Decimal("100")),
        ],
    )

    capacity_percentage_snapshot = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        validators=[
            MinValueValidator(Decimal("0")),
            MaxValueValidator(Decimal("100")),
        ],
    )

    eligible_user_count_at_acceptance = models.PositiveIntegerField(
        default=0,
    )

    capacity_limit_at_acceptance = models.PositiveIntegerField(
        default=0,
    )

    # This is a saved snapshot, not a live value from the Service.
    total_order_value = models.DecimalField(
        max_digits=22,
        decimal_places=8,
        default=Decimal("0.00000000"),
    )

    availability_status = models.CharField(
        max_length=20,
        choices=AvailabilityStatus.choices,
        default=AvailabilityStatus.AVAILABLE,
    )

    fulfillment_status = models.CharField(
        max_length=20,
        choices=FulfillmentStatus.choices,
        default=FulfillmentStatus.ACCEPTED,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(has_fulfillment_cost_snapshot=True) | models.Q(fulfillment_cost_per_1000_snapshot__isnull=True),
                name="order_cost_only_when_it_has_one",
            ),
            # Not conditional: rows with a NULL wallet or ID never collide (MySQL too).
            models.UniqueConstraint(fields=["admin_wallet", "external_order_id"], name="order_external_id_per_wallet"),
            models.CheckConstraint(
                condition=models.Q(charge_amount__isnull=True) | (models.Q(charge_amount__gt=0) & models.Q(admin_wallet__isnull=False)),
                name="order_charge_needs_wallet",
            ),
        ]
        indexes = [
            models.Index(
                fields=["service", "fulfillment_status"],
                name="order_service_fulfill_idx",
            ),
            models.Index(
                fields=["availability_status"],
                name="order_avail_status_idx",
            ),
            models.Index(
                fields=["created_at"],
                name="order_created_at_idx",
            ),
            # Duplicate-link checks and the worker exclusion look up (link, service).
            models.Index(fields=["normalized_link", "service"], name="order_link_service_idx"),
        ]

    def save(self, *args, **kwargs):
        from .links import canonical_link

        self.normalized_link = canonical_link(self.target_link)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "target_link" in update_fields:
            kwargs["update_fields"] = {*update_fields, "normalized_link"}
        super().save(*args, **kwargs)

    def __str__(self):
        return f"Order #{self.pk} - {self.service.name}"

    def fulfillment_cost_for(self, units):
        """Recorded fulfillment cost (INR) for `units` units, from this Order's snapshots.

        0 when the service had no fulfillment cost; None when it had one that
        was never recorded, so profit cannot be computed.
        """
        if self.fulfillment_cost_per_1000_snapshot is not None:
            return self.fulfillment_cost_per_1000_snapshot * units / Decimal("1000")
        return None if self.has_fulfillment_cost_snapshot else Decimal("0")


class AdminWallet(models.Model):
    """One per staff user: the prepaid INR balance that pays for the API orders placed with their personal key.

    The key is stored only as a SHA-256 hash (orders.api_keys). Orders placed
    with it (/api/v2 and the X-API-Key endpoints) are charged to `balance` at
    creation; the balance only changes together with an AdminWalletTransaction
    row (orders.admin_wallets). Worker wallets (wallet.Wallet) are separate.
    """

    # Staff only: created for every staff user (orders.signals and the data
    # migration) and refused for API use once its owner is no longer active staff.
    admin = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="admin_wallet")
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    # NULL until the admin generates a key on My Wallet; unique, and NULLs never collide.
    api_key_hash = models.CharField(max_length=64, unique=True, null=True, blank=True, editable=False)
    # First 8 characters of the key, so admins can tell keys apart. A key carried
    # over from the single Phase 6 key shows "…" and that key's last 4 instead.
    api_key_prefix = models.CharField(max_length=8, blank=True, default="")
    api_key_created_at = models.DateTimeField(null=True, blank=True)
    api_key_last_used_at = models.DateTimeField(null=True, blank=True)
    low_balance_threshold = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("500.00"), validators=[MinValueValidator(Decimal("0"))],
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # Alert bookkeeping (orders.admin_wallets). True while the balance is below
    # the threshold and that crossing has been announced.
    low_balance_alerted = models.BooleanField(default=False, editable=False)
    # Orders refused for insufficient funds and not yet announced, and when the
    # last announcement went out (at most one every 15 minutes).
    refused_orders_unreported = models.PositiveIntegerField(default=0, editable=False)
    refused_alert_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ["admin_id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(balance__gte=0), name="admin_wallet_balance_nonnegative"),
            models.CheckConstraint(condition=models.Q(low_balance_threshold__gte=0), name="admin_wallet_threshold_nonnegative"),
        ]

    def __str__(self):
        return f"Admin wallet #{self.pk} (admin #{self.admin_id})"


class AdminWalletTransaction(models.Model):
    """The ledger of an AdminWallet's balance: one row per change, never edited.

    balance == funds added + refunds + credit adjustments - order charges -
    debit adjustments (checked daily by reconcile_wallets).
    """

    class Type(models.TextChoices):
        ADD_FUNDS = "add_funds", "Funds added"
        ORDER_CHARGE = "order_charge", "Order charge"
        REFUND = "refund", "Refund"
        ADJUSTMENT = "adjustment", "Adjustment"

    class Direction(models.TextChoices):
        CREDIT = "credit", "Credit"
        DEBIT = "debit", "Debit"

    wallet = models.ForeignKey(AdminWallet, on_delete=models.PROTECT, related_name="transactions")
    transaction_type = models.CharField(max_length=20, choices=Type.choices)
    direction = models.CharField(max_length=6, choices=Direction.choices)
    # Always positive; direction says which way the balance moved.
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    balance_after = models.DecimalField(max_digits=14, decimal_places=2)
    order = models.ForeignKey(Order, on_delete=models.PROTECT, null=True, blank=True, related_name="admin_wallet_transactions")
    # The order again, only on its charge (or refund) row and unique: one
    # charge and one refund per order on every database, as nullable unique
    # columns (NULLs never collide), not conditional constraints.
    charge_order = models.OneToOneField(
        Order, on_delete=models.PROTECT, null=True, blank=True, editable=False, related_name="admin_wallet_charge",
    )
    refund_order = models.OneToOneField(
        Order, on_delete=models.PROTECT, null=True, blank=True, editable=False, related_name="admin_wallet_refund",
    )
    # Funds added: the UPI UTR (or other payment reference) the money arrived with.
    reference = models.CharField(max_length=100, blank=True, default="")
    note = models.CharField(max_length=500, blank=True, default="")
    # The form's request key, so a double-submitted entry is recorded once.
    request_key = models.UUIDField(null=True, blank=True, unique=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(condition=models.Q(amount__gt=0), name="admin_wallet_tx_amount_positive"),
            models.CheckConstraint(condition=models.Q(balance_after__gte=0), name="admin_wallet_tx_balance_nonnegative"),
            # Funds and refunds add, charges take away, adjustments go either way.
            models.CheckConstraint(
                condition=models.Q(transaction_type__in=["add_funds", "refund"], direction="credit")
                | models.Q(transaction_type="order_charge", direction="debit")
                | models.Q(transaction_type="adjustment"),
                name="admin_wallet_tx_type_direction",
            ),
            # order__isnull=False is explicit: with order NULL the equality is
            # unknown, and a CHECK that evaluates to unknown passes.
            models.CheckConstraint(
                condition=(models.Q(transaction_type="order_charge") & models.Q(order__isnull=False)
                           & models.Q(charge_order=models.F("order")))
                | (~models.Q(transaction_type="order_charge") & models.Q(charge_order__isnull=True)),
                name="admin_wallet_tx_charge_names_order",
            ),
            models.CheckConstraint(
                condition=(models.Q(transaction_type="refund") & models.Q(order__isnull=False)
                           & models.Q(refund_order=models.F("order")))
                | (~models.Q(transaction_type="refund") & models.Q(refund_order__isnull=True)),
                name="admin_wallet_tx_refund_names_order",
            ),
            models.CheckConstraint(
                condition=~models.Q(transaction_type="add_funds") | ~models.Q(reference=""),
                name="admin_wallet_tx_funds_reference",
            ),
            # Only charges and refunds name an order.
            models.CheckConstraint(
                condition=models.Q(transaction_type__in=["order_charge", "refund"]) | models.Q(order__isnull=True),
                name="admin_wallet_tx_order_only_on_orders",
            ),
        ]
        indexes = [
            models.Index(fields=["wallet", "created_at"], name="admin_wallet_tx_history_idx"),
            models.Index(fields=["wallet", "transaction_type", "created_at"], name="admin_wallet_tx_type_idx"),
        ]

    def __str__(self):
        return f"{self.get_transaction_type_display()} ₹{self.amount} - admin wallet #{self.wallet_id}"
