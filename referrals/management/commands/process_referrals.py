from django.core.management.base import BaseCommand

from referrals.services import process_referrals


class Command(BaseCommand):
    help = (
        "Daily referral upkeep: expire overdue pending referrals, re-check them for shared accounts, "
        "pay qualified referrals the monthly cap held back, and send the daily bonus summary."
    )

    def handle(self, *args, **options):
        result = process_referrals()
        self.stdout.write(f"Referrals: {result['expired']} expired, {result['flagged']} flagged, {result['paid']} paid.")
