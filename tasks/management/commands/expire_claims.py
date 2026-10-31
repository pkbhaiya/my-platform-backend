from django.core.management.base import BaseCommand

from common.notifications import flush_order_summaries, record_cron_run
from orders.admin_wallets import flush_refused_order_alerts
from tasks.services import expire_old_orders, expire_stale_claims


class Command(BaseCommand):
    help = (
        "Expire claims still `claimed` past the Site Configuration claim timeout and release their slots, "
        "then cancel Orders still open past the configured order maximum age (reason `expired`). "
        "Safe to run concurrently with requests; schedule it every minute in production, e.g. "
        "`* * * * * cd /srv/my-platform && .venv/bin/python manage.py expire_claims`."
    )

    def handle(self, *args, **options):
        record_cron_run()
        claims = expire_stale_claims()
        orders = expire_old_orders()
        # Busy new-order windows are summarised here once they close.
        flush_order_summaries()
        # Orders refused for insufficient funds within the last 15 minutes are summarised once it passes.
        flush_refused_order_alerts()
        self.stdout.write(f"Expired {claims} claim(s) and {orders} order(s).")
