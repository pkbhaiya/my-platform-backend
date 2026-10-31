from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0004_userlevel")]
    operations = [
        migrations.AddField("userlevel", "description", models.TextField(blank=True, default="")),
        migrations.AddField("userlevel", "benefits", models.TextField(blank=True, default="")),
        migrations.AddField("userlevel", "extra_earning_description", models.TextField(blank=True, default="")),
        migrations.AddField("userlevel", "demo_video_url", models.URLField(blank=True, default="", max_length=500)),
    ]
