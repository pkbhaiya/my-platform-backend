import re
from collections import defaultdict

from django.db import migrations

UPI_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]+@[A-Za-z0-9]+")


def username_key(value):
    return (value or "").strip().lstrip("@").strip().lower()


def platform_key(value):
    return " ".join((value or "").split()).lower()


def backfill(apps, schema_editor):
    """Fill the new unique keys from existing data without ever breaking a uniqueness rule.

    - Platform usernames: the earliest-registered account keeps a shared
      username; later accounts are left without a key and listed for review.
    - Payout UPI: each user's latest UPI withdrawal. A UPI ID that is the latest
      for more than one user is assigned to none of them and listed.
    """
    User = apps.get_model("accounts", "User")
    PlatformUsername = apps.get_model("accounts", "PlatformUsername")
    WithdrawalRequest = apps.get_model("wallet", "WithdrawalRequest")
    notes = []

    owners = {}
    for user in User.objects.order_by("date_joined", "pk"):
        key = username_key(user.platform_username)
        if not key:
            continue
        if key in owners:
            notes.append(f"user #{user.pk} shares its platform username with user #{owners[key]} (left unclaimed)")
            continue
        owners[key] = user.pk
        User.objects.filter(pk=user.pk).update(platform_username_key=key)

    claimed = set()
    for row in PlatformUsername.objects.order_by("created_at", "pk"):
        pair = (platform_key(row.platform_name), username_key(row.username))
        if not all(pair) or pair in claimed:
            notes.append(f"additional username #{row.pk} conflicts or is empty (left unclaimed)")
            continue
        claimed.add(pair)
        PlatformUsername.objects.filter(pk=row.pk).update(platform_key=pair[0], username_key=pair[1])

    latest = {}
    for user_id, details in WithdrawalRequest.objects.filter(payout_method="upi").order_by("requested_at", "pk").values_list("user_id", "payout_details"):
        latest[user_id] = (details or "").strip().lower()
    holders = defaultdict(list)
    for user_id, upi in latest.items():
        holders[upi].append(user_id)
    for upi, user_ids in holders.items():
        if len(user_ids) > 1:
            notes.append(f"UPI ID shared by users {sorted(user_ids)} (assigned to none)")
        elif not UPI_ID_PATTERN.fullmatch(upi) or len(upi) > 255:
            notes.append(f"user #{user_ids[0]}'s last payout details are not a valid UPI ID (not copied)")
        else:
            User.objects.filter(pk=user_ids[0]).update(upi_id=upi)

    for note in notes:
        print(f"\n  [accounts.0010] {note}")


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0009_abuse_protection"),
        ("wallet", "0005_reward_claim_unique_column"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
