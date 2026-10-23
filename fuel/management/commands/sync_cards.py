from django.core.management.base import BaseCommand

from fuel.card_service import sync_ukrnafta_cards


class Command(BaseCommand):
    help = "Оновлює статуси карток UkrNafta"

    def handle(self, *args, **opts):
        created, updated = sync_ukrnafta_cards()
        self.stdout.write(self.style.SUCCESS(f"UkrNafta: нових {created}, оновлено {updated}"))
