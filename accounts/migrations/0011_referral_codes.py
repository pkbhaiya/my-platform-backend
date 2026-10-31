import secrets

from django.db import migrations, models
from django.db.models import Q

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0, O, 1 or I


def backfill_codes(apps, schema_editor):
    """Give every existing user a unique 7-character referral code."""
    User = apps.get_model("accounts", "User")
    missing = Q(referral_code__isnull=True) | Q(referral_code="")
    used = set(User.objects.exclude(missing).values_list("referral_code", flat=True))
    for user_id in User.objects.filter(missing).values_list("pk", flat=True):
        code = "".join(secrets.choice(ALPHABET) for _ in range(7))
        while code in used:
            code = "".join(secrets.choice(ALPHABET) for _ in range(7))
        used.add(code)
        User.objects.filter(pk=user_id).update(referral_code=code)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0010_backfill_identity_keys"),
    ]

    operations = [
        # Nullable first, so existing rows do not collide on the unique index.
        migrations.AddField(
            model_name="user",
            name="referral_code",
            field=models.CharField(editable=False, max_length=8, null=True, unique=True),
        ),
        migrations.RunPython(backfill_codes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="user",
            name="referral_code",
            field=models.CharField(editable=False, max_length=8, unique=True),
        ),
    ]
