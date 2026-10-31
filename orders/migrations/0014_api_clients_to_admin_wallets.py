"""Replace the API clients (the first Phase 11) with one wallet per admin.

- Every staff user gets an AdminWallet.
- The single Phase 6 reseller key (carried into the API client "Default" by
  orders.0011) moves to the main superuser's wallet with its hash unchanged, so
  the current Perfect Panel connection keeps working.
- Every other API client goes to the wallet of the admin who created it (the
  main superuser when that admin is not staff). Its key moves too when that
  wallet has no key yet; otherwise the key stops working and this is printed.
- Ledgers are copied in time order (deposit -> add_funds); balance_after is
  recomputed per wallet, so a wallet that received several clients reconciles.
  Orders follow their client. Nothing is deleted here (orders.0015 drops the
  old tables). What was done is printed.
"""

from decimal import Decimal

from django.db import migrations

CREDIT, DEBIT = "credit", "debit"
TYPE_MAP = {"deposit": "add_funds", "order_charge": "order_charge", "refund": "refund", "adjustment": "adjustment"}


def say(message):
    print(f"[orders.0014] {message}")


def main_superuser(User):
    staff = User.objects.filter(is_staff=True).order_by("id")
    return (staff.filter(is_superuser=True, is_active=True).first() or staff.filter(is_superuser=True).first()
            or staff.filter(is_active=True).first() or staff.first())


def convert(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    ApiClient = apps.get_model("orders", "ApiClient")
    ApiClientTransaction = apps.get_model("orders", "ApiClientTransaction")
    AdminWallet = apps.get_model("orders", "AdminWallet")
    AdminWalletTransaction = apps.get_model("orders", "AdminWalletTransaction")
    Order = apps.get_model("orders", "Order")

    wallets = {}
    for user in User.objects.filter(is_staff=True).order_by("id"):
        # Balance 0 is below the default threshold: nothing to announce until it has been above it.
        wallets[user.pk], _ = AdminWallet.objects.get_or_create(admin=user, defaults={"low_balance_alerted": True})
    say(f"{len(wallets)} admin wallet(s) created, one per staff user.")

    clients = list(ApiClient.objects.order_by("id"))
    if not clients:
        say("No API clients to convert.")
        return
    main = main_superuser(User)
    if main is None:
        if ApiClientTransaction.objects.exists() or Order.objects.filter(api_client__isnull=False).exists():
            raise RuntimeError("API clients have balances or orders, but there is no staff user to own them. "
                               "Create a superuser (manage.py createsuperuser) at the previous migration, then migrate again.")
        say(f"{len(clients)} unused API client(s) without balance or orders dropped: there is no staff user to own them.")
        return

    # The Phase 6 key first, so it always lands on the main superuser's wallet.
    phase6 = next((client for client in clients if client.name == "Default" and client.key_prefix.startswith("…")), None)
    ordered = ([phase6] if phase6 else []) + [client for client in clients if client is not phase6]
    staff_ids = set(wallets)
    owner_of, keyed = {}, {}
    for client in ordered:
        owner_id = main.pk if client is phase6 or client.created_by_id not in staff_ids else client.created_by_id
        wallet = wallets[owner_id]
        owner_of[client.pk] = wallet
        label = f"API client #{client.pk} \"{client.name}\""
        if wallet.pk not in keyed and client.is_active:
            AdminWallet.objects.filter(pk=wallet.pk).update(
                api_key_hash=client.key_hash, api_key_prefix=client.key_prefix,
                api_key_created_at=client.key_regenerated_at or client.created_at, api_key_last_used_at=client.last_used_at,
                low_balance_threshold=client.low_balance_threshold,
            )
            keyed[wallet.pk] = client.pk
            kept = "the existing Phase 6 reseller key" if client is phase6 else "its key"
            say(f"{label} -> admin #{owner_id}'s wallet #{wallet.pk}: {kept} (prefix {client.key_prefix or '—'}) keeps working.")
        else:
            why = "the client was disabled" if not client.is_active else f"that wallet already holds API client #{keyed[wallet.pk]}'s key"
            say(f"{label} -> admin #{owner_id}'s wallet #{wallet.pk}: its key (prefix {client.key_prefix or '—'}) stops working "
                f"because {why}. Regenerate a key on My Wallet if it is still needed.")
        # External order IDs were unique per client; two clients merged into one
        # wallet could share one, which orders.0015's (wallet, ID) constraint refuses.
        taken = set(Order.objects.filter(admin_wallet=wallet).exclude(external_order_id=None).values_list("external_order_id", flat=True))
        for order in Order.objects.filter(api_client=client).exclude(external_order_id=None).filter(external_order_id__in=taken):
            renamed = f"{order.external_order_id}~client{client.pk}"[-100:]
            Order.objects.filter(pk=order.pk).update(external_order_id=renamed)
            say(f"Order #{order.pk}: external order ID shared with another client of wallet #{wallet.pk}; renamed to {renamed!r}.")
        linked = Order.objects.filter(api_client=client).update(admin_wallet=wallet)
        if linked:
            say(f"{label}: {linked} order(s) now belong to wallet #{wallet.pk}.")

    for wallet in {wallet.pk: wallet for wallet in owner_of.values()}.values():
        client_ids = [client_id for client_id, owner in owner_of.items() if owner.pk == wallet.pk]
        sources = ApiClient.objects.filter(pk__in=client_ids)
        running = Decimal("0.00")
        rows = ApiClientTransaction.objects.filter(client_id__in=client_ids).order_by("created_at", "id")
        copied = 0
        for row in rows:
            running += row.amount if row.direction == CREDIT else -row.amount
            new = AdminWalletTransaction.objects.create(
                wallet=wallet, transaction_type=TYPE_MAP[row.transaction_type], direction=row.direction, amount=row.amount,
                balance_after=running, order_id=row.order_id, charge_order_id=row.charge_order_id,
                refund_order_id=row.refund_order_id, reference=row.reference,
                note=row.note or (f"Moved from API client #{row.client_id}" if len(client_ids) > 1 else ""),
                request_key=row.request_key, created_by_id=row.created_by_id,
            )
            AdminWalletTransaction.objects.filter(pk=new.pk).update(created_at=row.created_at)
            copied += 1
        balance = sum((client.balance for client in sources), Decimal("0.00"))
        if balance != running:
            # A pre-existing mismatch: keep the money the clients actually held and record the difference.
            difference = balance - running
            running = balance
            AdminWalletTransaction.objects.create(
                wallet=wallet, transaction_type="adjustment", direction=CREDIT if difference > 0 else DEBIT,
                amount=abs(difference), balance_after=running,
                note="Migration: the API clients' balance did not match their ledger; the difference is recorded here.",
            )
            say(f"Wallet #{wallet.pk}: the clients' balances differed from their ledger by ₹{difference}; recorded as an adjustment.")
        threshold = AdminWallet.objects.values_list("low_balance_threshold", flat=True).get(pk=wallet.pk)
        AdminWallet.objects.filter(pk=wallet.pk).update(
            balance=running, low_balance_alerted=running < threshold,
            refused_orders_unreported=sum(client.refused_orders_unreported for client in sources),
        )
        say(f"Wallet #{wallet.pk}: balance ₹{running}, {copied} ledger row(s) copied from API client(s) {client_ids}.")


class Migration(migrations.Migration):

    dependencies = [
        ("orders", "0013_admin_wallets"),
        ("accounts", "0015_gender_male_female"),
    ]

    operations = [
        migrations.RunPython(convert, migrations.RunPython.noop),
    ]
