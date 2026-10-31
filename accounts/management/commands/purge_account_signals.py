from django.core.management.base import BaseCommand

from accounts.identity import purge_old_login_ips


class Command(BaseCommand):
    help = (
        "Delete login-IP account signals not seen for 180 days (data retention). "
        "Run daily, e.g. with reconcile_wallets at 03:00 IST."
    )

    def handle(self, *args, **options):
        deleted = purge_old_login_ips()
        self.stdout.write(f"Deleted {deleted} login IP signal(s) older than 180 days.")
