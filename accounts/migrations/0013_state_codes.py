from django.db import migrations, models

from accounts.constants import STATE_CHOICES, state_code_for


def mapped_states(rows):
    """For (user_id, free-text state) rows: ({user_id: code or None}, [(user_id, value) left unmapped])."""
    changes, unmapped = {}, []
    for user_id, value in rows:
        code = state_code_for(value)
        changes[user_id] = code
        if code is None and (value or "").strip():
            unmapped.append((user_id, value))
    return changes, unmapped


def free_text_to_codes(apps, schema_editor):
    """A stored state that matches a name (ignoring case and spacing) becomes its code; anything else becomes NULL."""
    User = apps.get_model("accounts", "User")
    changes, unmapped = mapped_states(list(User.objects.values_list("pk", "state")))
    for user_id, code in changes.items():
        User.objects.filter(pk=user_id).update(state=code)
    for user_id, value in unmapped:
        print(f"\n  [accounts.0013] User #{user_id}: state {value!r} matches no State/UT name; left empty.", end="")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0012_referral_code_eight_chars"),
    ]

    operations = [
        # Nullable first, so unmatched values can be cleared.
        migrations.AlterField(
            model_name="user",
            name="state",
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.RunPython(free_text_to_codes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="user",
            name="state",
            field=models.CharField(blank=True, choices=list(STATE_CHOICES), max_length=50, null=True),
        ),
    ]
