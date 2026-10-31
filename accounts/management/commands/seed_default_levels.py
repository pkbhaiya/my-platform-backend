from django.core.management.base import BaseCommand
from django.db import transaction

from accounts.models import UserLevel


DEFAULT_LEVELS = [
    {
        "code": "bronze",
        "name": "Bronze",
        "minimum_approved_tasks": 0,
        "display_order": 1,
        "badge_color": "#CD7F32",
    },
    {
        "code": "silver",
        "name": "Silver",
        "minimum_approved_tasks": 50,
        "display_order": 2,
        "badge_color": "#94A3B8",
    },
    {
        "code": "gold",
        "name": "Gold",
        "minimum_approved_tasks": 100,
        "display_order": 3,
        "badge_color": "#D4A017",
    },
    {
        "code": "diamond",
        "name": "Diamond",
        "minimum_approved_tasks": 200,
        "display_order": 4,
        "badge_color": "#0EA5E9",
    },
]


class Command(BaseCommand):
    help = "Create default user levels without overwriting existing settings."

    @transaction.atomic
    def handle(self, *args, **options):
        created_count = 0

        for level_data in DEFAULT_LEVELS:
            _, created = UserLevel.objects.get_or_create(
                code=level_data["code"],
                defaults=level_data,
            )

            if created:
                created_count += 1
                self.stdout.write(
                    self.style.SUCCESS(
                        f"Created level: {level_data['name']}"
                    )
                )
            else:
                self.stdout.write(
                    f"Already exists: {level_data['name']}"
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Finished. Newly created levels: {created_count}"
            )
        )