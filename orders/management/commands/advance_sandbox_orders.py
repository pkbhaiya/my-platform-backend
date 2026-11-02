from django.core.management.base import BaseCommand

from orders.sandbox import advance_sandbox_orders


class Command(BaseCommand):
    help = (
        "Move sandbox (test) orders on: In progress 1 minute after creation, Completed or Partial after 3 "
        "(see orders.sandbox). expire_claims does this too; this command is for servers that schedule only this step."
    )

    def handle(self, *args, **options):
        self.stdout.write(f"Moved {advance_sandbox_orders()} sandbox order(s) on.")
