import decimal
from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("orders", "0003_order_idempotency_key")]
    operations = [migrations.AddField(
        model_name="order",
        name="fulfillment_cost_per_1000_snapshot",
        field=models.DecimalField(blank=True, decimal_places=4, max_digits=14, null=True, validators=[MinValueValidator(decimal.Decimal("0"))]),
    )]
