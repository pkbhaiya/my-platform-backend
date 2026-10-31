from django.core.management.base import BaseCommand

from wallet.reconciliation import reconcile_wallets


class Command(BaseCommand):
    help = (
        "Check every wallet against its ledger, claims and withdrawals, and every admin wallet's balance "
        "against its charges, refunds, funds added and adjustments; store the result and alert on "
        "mismatches. Read-only: balances are never corrected. Run daily at 03:00 IST."
    )

    def handle(self, *args, **options):
        run = reconcile_wallets()
        notices = len(run.details.get("notices", []))
        self.stdout.write(
            f"Reconciliation run #{run.pk}: {run.wallets_checked} wallet(s) and "
            f"{run.details.get('admin_wallets_checked', 0)} admin wallet(s) checked, "
            f"{run.mismatches_count} mismatch(es), {notices} notice(s)."
        )
