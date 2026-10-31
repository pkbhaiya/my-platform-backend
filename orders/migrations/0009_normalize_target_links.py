"""Normalise every stored Order and Task target_link the way new orders are normalised.

Trimmed; "https://" added when there is no scheme; refused unless it is then an
http(s) URL. Changed rows are printed. A value that still is not a valid link
is left exactly as it is and listed for review, never guessed. The rules are
copied from orders/links.py so this migration keeps working if that changes.
"""

import re

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import migrations

HTTP_PREFIX = re.compile(r"^(https?)://", re.IGNORECASE)
OTHER_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*:(?!\d)", re.IGNORECASE)
validate = URLValidator(schemes=["http", "https"])


def normalized(value):
    text = (value or "").strip()
    if not text:
        return ""
    match = HTTP_PREFIX.match(text)
    if match:
        text = match.group(1).lower() + text[len(match.group(1)):]
    elif text.startswith("//"):
        text = f"https:{text}"
    elif OTHER_SCHEME.match(text):
        return None
    else:
        text = f"https://{text}"
    if len(text) > 2048:
        return None
    try:
        validate(text)
    except ValidationError:
        return None
    return text


def normalize_target_links(apps, schema_editor):
    for app_label, model_name in (("orders", "Order"), ("tasks", "Task")):
        model = apps.get_model(app_label, model_name)
        for pk, link in model.objects.order_by("pk").values_list("pk", "target_link"):
            fixed = normalized(link)
            if fixed is None:
                print(f"  [orders.0009] {model_name} #{pk}: target link is not a valid http(s) URL; left unchanged for review.")
            elif fixed != link:
                model.objects.filter(pk=pk).update(target_link=fixed)
                print(f"  [orders.0009] {model_name} #{pk}: target link normalised (scheme or spaces fixed).")


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0008_incoming_api_key_hash"),
        ("tasks", "0016_maintenance_mode"),
    ]

    operations = [
        # Not reversible from data alone (the original spacing and scheme are not kept).
        migrations.RunPython(normalize_target_links, migrations.RunPython.noop),
    ]
