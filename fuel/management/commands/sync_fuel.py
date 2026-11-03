from argparse import RawDescriptionHelpFormatter
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError, DjangoHelpFormatter
from django.utils import timezone

from fuel.models import MAX_SYNC_DAYS, ProviderConfig
from fuel.providers import PROVIDERS
from fuel.services import (
    MODE_DAYS,
    MODE_PERIOD,
    SyncMessage,
    build_periods,
    sync_periods_chunked,
)

USAGE_EXAMPLES = f"""Приклади:
  python manage.py sync_fuel --days 2
      усі провайдери, останні 2 дні (разом із сьогодні)
  python manage.py sync_fuel --days 90 --provider upg --provider ukrnafta
      вибрані провайдери, 90 днів
  python manage.py sync_fuel --config-days
      кожен провайдер на свою глибину «Глибина синхронізації, днів» з «Конфігурації провайдера»
  python manage.py sync_fuel --config-period --provider petroline
      період «Синхронізувати з / по» з «Конфігурації провайдера»

Провайдери: {", ".join(PROVIDERS)}. Ключі --days / --config-days / --config-period взаємовиключні.
Період довший за 30 днів завантажується частинами по 30 днів з паузою 2 с; максимум {MAX_SYNC_DAYS} днів."""


class _HelpFormatter(DjangoHelpFormatter, RawDescriptionHelpFormatter):
    """Довідка Django, але приклади в epilog виводяться без переформатування."""


class Command(BaseCommand):
    help = "Завантажує транзакції від провайдерів у базу"

    def create_parser(self, prog_name, subcommand, **kwargs):
        parser = super().create_parser(prog_name, subcommand, epilog=USAGE_EXAMPLES, **kwargs)
        parser.formatter_class = _HelpFormatter
        original_error = parser.error

        def error_with_examples(message):
            original_error(f"{message}\n\n{USAGE_EXAMPLES}")

        parser.error = error_with_examples
        return parser

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument("--days", type=int, help="Скільки днів назад (разом із сьогодні), типово 2")
        mode.add_argument(
            "--config-days",
            action="store_true",
            help="Глибина в днях для кожного провайдера з «Конфігурації провайдера»",
        )
        mode.add_argument(
            "--config-period",
            action="store_true",
            help="Період «з/по» для кожного провайдера з «Конфігурації провайдера»",
        )
        parser.add_argument(
            "--provider",
            choices=list(PROVIDERS),
            action="append",
            help="Провайдер (можна повторювати); типово всі",
        )

    def handle(self, *args, **opts):
        names = opts["provider"] or list(PROVIDERS)
        if opts["config_days"] or opts["config_period"]:
            mode = MODE_DAYS if opts["config_days"] else MODE_PERIOD
            configs = ProviderConfig.ensure_all()
            periods, messages = build_periods([configs[name] for name in names], mode)
        else:
            days = 2 if opts["days"] is None else opts["days"]
            if not 1 <= days <= MAX_SYNC_DAYS:
                raise CommandError(
                    f"--days має бути від 1 до {MAX_SYNC_DAYS}, отримано {days}\n\n{USAGE_EXAMPLES}"
                )
            today = timezone.localdate()
            periods = {name: (today - timedelta(days=days), today) for name in names}
            messages = []

        self._write(messages)
        if not periods:
            raise CommandError(f"Немає провайдерів для синхронізації\n\n{USAGE_EXAMPLES}")
        self.stdout.write(f"Синхронізація: {', '.join(periods)}")
        self._write(sync_periods_chunked(periods))

    def _write(self, messages: list[SyncMessage]) -> None:
        styles = {
            "success": self.style.SUCCESS,
            "warning": self.style.WARNING,
            "error": self.style.ERROR,
        }
        for message in messages:
            self.stdout.write(styles.get(message.level, str)(message.text))
