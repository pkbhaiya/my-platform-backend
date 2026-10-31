import decimal
from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("services_catalog", "0002_service_service_price_positive_and_more")]
    operations = [migrations.AddField(
        model_name="service",
        name="fulfillment_cost_per_1000",
        field=models.DecimalField(blank=True, decimal_places=4, help_text="Optional recorded fulfillment cost in INR per 1,000 units. Leave blank until verified.", max_digits=14, null=True, validators=[MinValueValidator(decimal.Decimal("0"))]),
    )]
