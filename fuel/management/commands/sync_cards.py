from django.core.management.base import BaseCommand, CommandError

from fuel.card_service import SYNCERS, sync_cards
from fuel.models import AuditLog, Provider


class Command(BaseCommand):
    help = "Оновлює картки провайдера з його API (типово UkrNafta); результат пишеться в журнал аудиту"

    def add_arguments(self, parser):
        parser.add_argument(
            "--provider",
            choices=list(SYNCERS),
            default=Provider.UKRNAFTA.value,
            help="Провайдер; типово ukrnafta",
        )

    def handle(self, *args, **opts):
        provider = opts["provider"]
        try:
            created, updated = sync_cards(provider, source=AuditLog.SOURCE_COMMAND)
        except ValueError as exc:
            raise CommandError(f"{provider}: {exc}") from exc
        self.stdout.write(self.style.SUCCESS(f"{provider}: нових {created}, оновлено {updated}"))
