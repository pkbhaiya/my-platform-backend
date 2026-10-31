"""Move the level ladder to Bronze -> Silver -> Gold -> Diamond.

Levels are derived from approved claim counts, so no user progress is stored or
lost here. The existing entry level (code "starter") becomes "bronze" and every
service that referenced it is repointed. Silver is inserted between Bronze and
Gold with a default threshold of 50 approved claims; admins can edit it later.
Safe to re-run: every step checks the current state first.
"""

from django.db import migrations

ORDER = ["bronze", "silver", "gold", "diamond"]
DEFAULTS = {
    "bronze": {"name": "Bronze", "minimum_approved_tasks": 0, "badge_color": "#CD7F32"},
    "silver": {"name": "Silver", "minimum_approved_tasks": 50, "badge_color": "#94A3B8"},
    "gold": {"name": "Gold", "minimum_approved_tasks": 100, "badge_color": "#D4A017"},
    "diamond": {"name": "Diamond", "minimum_approved_tasks": 200, "badge_color": "#0EA5E9"},
}


def forwards(apps, schema_editor):
    UserLevel = apps.get_model("accounts", "UserLevel")
    Service = apps.get_model("services_catalog", "Service")
    alias = schema_editor.connection.alias
    levels = UserLevel.objects.using(alias)
    if not levels.exists():
        # Fresh install: nothing to transition; seed_default_levels creates the ladder.
        return

    starter = levels.filter(code="starter").first()
    if starter and not levels.filter(code="bronze").exists():
        starter.code = "bronze"
        if starter.name == "Starter":
            starter.name = "Bronze"
        starter.save(update_fields=["code", "name"])
        Service.objects.using(alias).filter(eligible_level="starter").update(eligible_level="bronze")

    # Free display_order values (unique) before assigning the final ladder order.
    for level in levels.all():
        levels.filter(pk=level.pk).update(display_order=1000 + level.pk)

    used_thresholds = set(levels.values_list("minimum_approved_tasks", flat=True))
    for code in ORDER:
        if levels.filter(code=code).exists():
            continue
        defaults = dict(DEFAULTS[code])
        threshold = defaults["minimum_approved_tasks"]
        while threshold in used_thresholds:
            threshold += 1
        defaults["minimum_approved_tasks"] = threshold
        used_thresholds.add(threshold)
        levels.create(code=code, display_order=900 + ORDER.index(code), is_active=True, **defaults)

    for index, code in enumerate(ORDER, start=1):
        levels.filter(code=code).update(display_order=index)
    others = levels.exclude(code__in=ORDER).order_by("minimum_approved_tasks", "pk")
    for offset, level in enumerate(others, start=len(ORDER) + 1):
        levels.filter(pk=level.pk).update(display_order=offset)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0005_userlevel_benefits_and_descriptions"),
        ("services_catalog", "0004_alter_service_eligible_level"),
    ]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
