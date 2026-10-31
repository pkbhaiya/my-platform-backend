"""Turn the single reseller key into the API client "Default" (balance ₹0.00).

The key's hash is copied as it is, so Perfect Panel (and any X-API-Key caller)
keeps working with the key it already has. Its first 8 characters were never
stored, so the client shows "…" and the old key's last 4 instead until the
key is regenerated. Every existing API order is linked to Default so `status`
keeps answering for it; those orders were never charged, so charge_amount stays
NULL and they are never refunded. What was done is printed.
"""

import hashlib
import secrets
from decimal import Decimal

from django.db import migrations


def create_default_client(apps, schema_editor):
    IncomingApiKey = apps.get_model("orders", "IncomingApiKey")
    ApiClient = apps.get_model("orders", "ApiClient")
    Order = apps.get_model("orders", "Order")

    key = IncomingApiKey.objects.filter(pk=1).first()
    legacy = Order.objects.filter(source="api", api_client__isnull=True)
    if key is None and not legacy.exists():
        print("[orders.0011] No reseller API key and no API orders: no Default client was created.")
        return

    fields = dict(name="Default", balance=Decimal("0.00"), low_balance_threshold=Decimal("500.00"), is_active=True,
                  # Balance 0 is below the threshold from the start; it is announced after the first top-up falls back.
                  low_balance_alerted=True)
    if key is not None:
        client = ApiClient.objects.create(key_prefix=f"…{key.key_hint}", key_hash=key.key_hash,
                                          created_by_id=key.rotated_by_id, key_regenerated_at=key.rotated_at, **fields)
        print(f"[orders.0011] API client #{client.pk} \"Default\" created from the existing reseller key "
              f"(ends …{key.key_hint}), balance ₹0.00. The same key keeps working; add funds before new orders "
              f"can be accepted.")
    else:
        # API orders but no key: a hash nobody holds, so Default exists for them; regenerate its key to use it.
        client = ApiClient.objects.create(key_prefix="", key_hash=hashlib.sha256(secrets.token_bytes(32)).hexdigest(), **fields)
        print(f"[orders.0011] API client #{client.pk} \"Default\" created for existing API orders. There was no "
              f"reseller key, so it has none that works: regenerate its key on the API Clients page.")
    linked = legacy.update(api_client=client)
    print(f"[orders.0011] {linked} existing API order(s) linked to API client #{client.pk} "
          f"(placed before prepaid balances: not charged, never refunded).")


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0010_api_clients"),
    ]

    operations = [
        migrations.RunPython(create_default_client, migrations.RunPython.noop),
    ]
