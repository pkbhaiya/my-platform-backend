from decimal import Decimal

from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models


class Service(models.Model):
    """A reusable service configuration for incoming orders."""

    class EligibleLevel(models.TextChoices):
        ALL = "all", "All Levels"
        BRONZE = "bronze", "Bronze"
        SILVER = "silver", "Silver"
        GOLD = "gold", "Gold"
        DIAMOND = "diamond", "Diamond"

    class OrderSource(models.TextChoices):
        API = "api", "API Only"
        MANUAL = "manual", "Manual Only"
        BOTH = "both", "API and Manual"

    class TargetType(models.TextChoices):
        POST = "post", "Post"
        PROFILE = "profile", "Profile"
        VIDEO = "video", "Video"
        SURVEY = "survey", "Survey"
        OTHER = "other", "Other"

    # Automatically generated numeric Service ID.
    service_id = models.BigAutoField(
        primary_key=True,
    )

    name = models.CharField(max_length=150)

    description = models.TextField(blank=True)

    # Grouping shown to reseller panels (Perfect Panel "category").
    category = models.CharField(max_length=100, default="General")

    # What the worker opens ("Open Post", "Open Profile", ...). Snapshotted onto
    # each Order so a later change does not relabel existing work.
    target_type = models.CharField(
        max_length=10,
        choices=TargetType.choices,
        default=TargetType.POST,
    )

    # Price charged per 1,000 units.
    price_per_1000 = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        validators=[
            MinValueValidator(Decimal("0.0001")),
        ],
        help_text="Price in INR for every 1,000 units.",
    )
    fulfillment_cost_per_1000 = models.DecimalField(
        max_digits=14,
        decimal_places=4,
        null=True,
        blank=True,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="Optional recorded fulfillment cost in INR per 1,000 units. Leave blank until verified.",
    )
    # False: the service has no fulfillment cost, so profit uses a cost of 0.
    # True: a cost exists; while fulfillment_cost_per_1000 is blank, profit is unavailable.
    has_fulfillment_cost = models.BooleanField(
        default=False,
        help_text="Tick when this service has a fulfillment cost (excluding user rewards).",
    )

    minimum_order_quantity = models.PositiveIntegerField(
        default=10,
        validators=[MinValueValidator(1)],
    )

    # The stable per-order maximum published to reseller panels. An order is
    # still refused when it exceeds the live capacity limit below.
    max_order_quantity = models.PositiveIntegerField(
        default=1000,
        validators=[MinValueValidator(1)],
        help_text="Largest quantity published to reseller panels. Must be at least the minimum order quantity.",
    )

    # Maximum permitted incoming order quantity is calculated
    # dynamically from the eligible active user count.
    capacity_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("70.00"),
        validators=[
            MinValueValidator(Decimal("0")),
            MaxValueValidator(Decimal("100")),
        ],
    )

    eligible_level = models.CharField(
        max_length=30,
        default=EligibleLevel.ALL,
    )

    # Never entered directly: save() derives it as 100 - user_reward_percentage.
    # Kept (and snapshotted onto Orders) for historical reporting.
    business_share_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("75.00"),
        editable=False,
        validators=[
            MinValueValidator(Decimal("0")),
            MaxValueValidator(Decimal("100")),
        ],
    )

    user_reward_percentage = models.DecimalField(
        max_digits=5,
        decimal_places=2,
        default=Decimal("25.00"),
        validators=[
            MinValueValidator(Decimal("0")),
            MaxValueValidator(Decimal("100")),
        ],
    )

    order_source = models.CharField(
        max_length=10,
        choices=OrderSource.choices,
        default=OrderSource.BOTH,
    )

    target_link_required = models.BooleanField(default=True)

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Service"
        verbose_name_plural = "Services"
        constraints = [
            models.CheckConstraint(
                condition=models.Q(price_per_1000__gt=0),
                name="service_price_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(has_fulfillment_cost=True) | models.Q(fulfillment_cost_per_1000__isnull=True),
                name="service_cost_only_when_it_has_one",
            ),
            models.CheckConstraint(
                condition=models.Q(minimum_order_quantity__gte=1),
                name="service_min_qty_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(max_order_quantity__gte=models.F("minimum_order_quantity")),
                name="service_max_qty_gte_min",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(capacity_percentage__gte=0)
                    & models.Q(capacity_percentage__lte=100)
                ),
                name="service_capacity_pct_range",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(business_share_percentage__gte=0)
                    & models.Q(business_share_percentage__lte=100)
                ),
                name="service_business_pct_range",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(user_reward_percentage__gte=0)
                    & models.Q(user_reward_percentage__lte=100)
                ),
                name="service_reward_pct_range",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    business_share_percentage=(
                        100 - models.F("user_reward_percentage")
                    )
                ),
                name="service_pct_sum_100",
            ),
        ]

    def clean(self):
        super().clean()

        if (
            self.max_order_quantity is not None
            and self.minimum_order_quantity is not None
            and self.max_order_quantity < self.minimum_order_quantity
        ):
            raise ValidationError({
                "max_order_quantity": "The maximum order quantity must be at least the minimum order quantity.",
            })

        if not self.has_fulfillment_cost and self.fulfillment_cost_per_1000 is not None:
            raise ValidationError({
                "fulfillment_cost_per_1000": "Tick 'Has fulfillment cost' to record a cost, or leave the cost blank.",
            })

        if (
            self.business_share_percentage is not None
            and self.user_reward_percentage is not None
            and (
                self.business_share_percentage
                + self.user_reward_percentage
                != Decimal("100.00")
            )
        ):
            raise ValidationError({
                "user_reward_percentage": (
                    "Business share and user reward must total 100%."
                )
            })

    def save(self, *args, **kwargs):
        # The business share is whatever the user reward leaves of 100%, so the
        # two can never disagree (service_pct_sum_100 stays as the DB backstop).
        if self.user_reward_percentage is not None:
            self.business_share_percentage = Decimal("100") - Decimal(self.user_reward_percentage)
            update_fields = kwargs.get("update_fields")
            if update_fields is not None and "user_reward_percentage" in update_fields:
                kwargs["update_fields"] = {*update_fields, "business_share_percentage"}
        # Validate fields and business rules before saving.
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.service_id} - {self.name}"
