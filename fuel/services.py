import logging
from datetime import date

from django.db import transaction

from .models import Card, Transaction
from .providers import PROVIDERS
from .providers.base import NormalizedTx

log = logging.getLogger(__name__)


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
    """Повертає {provider: (created, updated) | 'error: ...'}; збій одного не зупиняє інших."""
    results = {}
    for name in provider_names:
        print(f"----------------------------------------")
        print(f"-----PROVIDER: {name}")
        try:
            items = PROVIDERS[name]().fetch(date_from, date_to)
            results[name] = save_transactions(items)
        except Exception as exc:  # noqa: BLE001
            log.exception("Sync failed for %s", name)
            results[name] = f"error: {exc}"
    return results
