from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("services_catalog", "0003_service_fulfillment_cost_per_1000")]
    operations = [migrations.AlterField(
        model_name="service",
        name="eligible_level",
        field=models.CharField(default="all", max_length=30),
    )]
