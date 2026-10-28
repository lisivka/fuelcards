import logging
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .models import Card, ProviderConfig, Transaction
from .providers import PROVIDERS
from .providers.base import NormalizedTx

log = logging.getLogger(__name__)

DEFAULT_DAYS = 2  # якщо транзакцій провайдера в базі ще немає
MAX_CATCHUP_DAYS = 31  # межа «догонки» для кнопки; довший період: sync_fuel --days N


def save_transactions(items: list[NormalizedTx]) -> tuple[int, int]:
    created = updated = 0
    with transaction.atomic():
        for it in items:
            card = None
            if it.card_ref:
                card, _ = Card.objects.get_or_create(
                    provider=it.provider, number=it.card_ref, defaults={"vehicle": it.vehicle[:120]}
                )
            _, was_created = Transaction.objects.update_or_create(
                provider=it.provider,
                external_id=it.external_id,
                defaults={
                    "kind": it.kind,
                    "card": card,
                    "card_ref": it.card_ref,
                    "vehicle_raw": it.vehicle[:200],
                    "timestamp": it.timestamp,
                    "fuel": it.fuel,
                    "liters": it.liters,
                    "price": it.price,
                    "amount": it.amount,
                    "station": it.station,
                    "canceled": it.canceled,
                    "raw": it.raw,
                },
            )
            created += was_created
            updated += not was_created
    return created, updated


def run_sync(provider_names: list[str], date_from: date, date_to: date) -> dict:
    """Повертає {provider: (created, updated) | None | 'error: ...'}.

    None означає, що провайдера пропущено, бо він неактивний.
    Збій одного провайдера не зупиняє інших.
    """
    configs = ProviderConfig.ensure_all()
    results = {}
    for name in provider_names:
        config = configs.get(name)
        if config is not None and not config.is_active:
            log.info("Провайдер %s неактивний, пропускаємо", name)
            results[name] = None
            continue
        log.info("Синхронізація %s: %s .. %s", name, date_from, date_to)
        try:
            items = PROVIDERS[name]().fetch(date_from, date_to)
            results[name] = save_transactions(items)
            log.info("Синхронізація %s: нових %s, оновлено %s", name, *results[name])
        except Exception as exc:  # noqa: BLE001
            log.exception("Sync failed for %s", name)
            results[name] = f"error: {exc}"
    return results


def sync_since_last() -> tuple[dict, list[str]]:
    """Догружає кожному провайдеру дані від дня його останньої транзакції до сьогодні.

    Повертає (результати run_sync по провайдерах, провайдери, чий старт обмежено MAX_CATCHUP_DAYS).
    """
    today = timezone.localdate()
    earliest = today - timedelta(days=MAX_CATCHUP_DAYS)
    results: dict = {}
    capped: list[str] = []
    for name in PROVIDERS:
        last = Transaction.objects.filter(provider=name).aggregate(last=Max("timestamp"))["last"]
        if last is None:
            date_from = today - timedelta(days=DEFAULT_DAYS)
        else:
            date_from = timezone.localtime(last).date()
        was_capped = date_from < earliest
        if was_capped:
            date_from = earliest
        results[name] = run_sync([name], date_from, today)[name]
        if was_capped and isinstance(results[name], tuple):
            capped.append(name)
    return results, capped