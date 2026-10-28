from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from fuel.providers import PROVIDERS
from fuel.services import run_sync


class Command(BaseCommand):
    help = "Завантажує транзакції від провайдерів у базу"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=2, help="Скільки днів назад (разом із сьогодні)")
        parser.add_argument("--provider", choices=list(PROVIDERS), action="append")

    def handle(self, *args, **opts):
        today = timezone.localdate()
        date_from = today - timedelta(days=opts["days"])
        names = opts["provider"] or list(PROVIDERS)
        self.stdout.write(f"Синхронізація {date_from} .. {today}: {', '.join(names)}")
        for name, res in run_sync(names, date_from, today).items():
            if res is None:
                self.stdout.write(self.style.WARNING(f"{name}: пропущено (провайдер неактивний)"))
            elif isinstance(res, tuple):
                self.stdout.write(self.style.SUCCESS(f"{name}: нових {res[0]}, оновлено {res[1]}"))
            else:
                self.stdout.write(self.style.ERROR(f"{name}: {res}"))