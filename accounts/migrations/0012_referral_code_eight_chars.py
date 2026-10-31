import secrets

import django.db.models.functions.text
import django.db.models.lookups
from django.db import migrations

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0, O, 1 or I
LENGTH = 8


def lengthened_codes(rows):
    """{user_id: new_code} for the (user_id, code) rows whose code is not 8 characters.

    A shorter code keeps its characters and gains random ones at the end, so it
    stays recognisable; a missing code gets a new one. New codes never repeat
    any existing or other new code.
    """
    used = {code for _user_id, code in rows if code}
    changes = {}
    for user_id, code in rows:
        if code and len(code) == LENGTH:
            continue
        base = (code or "")[:LENGTH]
        new_code = base + "".join(secrets.choice(ALPHABET) for _ in range(LENGTH - len(base)))
        while new_code in used:
            new_code = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
        used.add(new_code)
        changes[user_id] = new_code
    return changes


def lengthen_codes(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    rows = list(User.objects.order_by("pk").values_list("pk", "referral_code"))
    for user_id, code in lengthened_codes(rows).items():
        User.objects.filter(pk=user_id).update(referral_code=code)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0011_referral_codes"),
    ]

    operations = [
        migrations.RunPython(lengthen_codes, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="user",
            constraint=django.db.models.CheckConstraint(
                condition=django.db.models.lookups.Exact(
                    django.db.models.functions.text.Length("referral_code"), 8
                ),
                name="user_referral_code_length",
            ),
        ),
    ]
