from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("wallet", "0003_remove_wallettransaction_uniq_task_reward_ledger_entry_and_more")]
    operations = [migrations.AlterField(
        model_name="withdrawalrequest",
        name="payout_method",
        field=models.CharField(choices=[("upi", "UPI")], max_length=10),
    )]
