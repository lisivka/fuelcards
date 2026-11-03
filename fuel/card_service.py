import logging
from collections import Counter

from django.db import transaction
from django.utils import timezone

from .models import Card, CardStatus, Provider, ProviderConfig
from .providers.petroline_cards import PetrolineCards
from .providers.ukrnafta_cards import UkrnaftaCards

log = logging.getLogger(__name__)

# Провайдери, для яких реалізоване керування картками через API
CLIENTS = {Provider.UKRNAFTA.value: UkrnaftaCards}


def editable_map() -> dict:
    """{code: True/False}: чи можна змінювати картки провайдера через API."""
    configs = ProviderConfig.ensure_all()
    return {code: bool(cfg.is_active and cfg.edit_enabled and code in CLIENTS) for code, cfg in configs.items()}


def split_name(full: str) -> tuple[str, str]:
    """ПІБ -> (прізвище, ім'я); по батькові в API не передається."""
    parts = full.split()
    return (parts[0] if parts else ""), (parts[1] if len(parts) > 1 else "")


def serialize(card: Card, editable: dict | None = None) -> dict:
    editable = editable if editable is not None else editable_map()
    return {
        "id": card.pk,
        "provider": card.provider,
        "provider_label": card.get_provider_display(),
        "number": card.number,
        "ext_number": card.ext_number,
        "display_number": card.display_number,
        "vehicle": card.vehicle,
        "driver_name": card.driver_name,
        "status": card.status,
        "status_label": card.get_status_display() if card.status is not None else "—",
        "status_updated": card.status_updated_at,
        "editable": editable.get(card.provider, False),
    }


def sync_ukrnafta_cards() -> tuple[int, int]:
    """Підтягує статуси карток UkrNafta. Порожні Авто/ПІБ заповнюються з API, заповнені не чіпаємо."""
    created = updated = 0
    now = timezone.now()
    for row in UkrnaftaCards().list_cards():
        number = str(row.get("cardId") or "").strip()
        if not number:
            continue
        info = row.get("cardInfo") or {}
        name = " ".join(p for p in (info.get("lastName"), info.get("firstName")) if p)
        vin = (row.get("vin") or "")
        card, was_created = Card.objects.get_or_create(
            provider=Provider.UKRNAFTA.value, number=number,
            defaults={"vehicle": vin[:120], "driver_name": name[:120]},
        )
        if not was_created:
            if not card.vehicle and vin:
                card.vehicle = vin[:120]
            if not card.driver_name and name:
                card.driver_name = name[:120]
        card.status = row.get("status")
        card.status_updated_at = now
        card.save()
        created += was_created
        updated += not was_created
    return created, updated


def sync_petroline_cards() -> tuple[int, int]:
    """Підтягує картки Petroline (лише читання): номер на картці, id, статус.

    Ключ картки: Ідентифікатор (`number`). Порожнє Авто заповнюється назвою з Petroline,
    заповнені Авто/ПІБ не чіпаємо.
    """
    rows = PetrolineCards().list_cards()
    unknown = Counter(row["api_status"] for row in rows if row["status"] is None)
    if unknown:
        log.warning("Petroline: невідомі коди статусу карток: %s", dict(unknown))
    created = updated = 0
    now = timezone.now()
    with transaction.atomic():
        for row in rows:
            card, was_created = Card.objects.get_or_create(
                provider=Provider.PETROLINE.value, number=row["number"],
                defaults={"vehicle": row["label"][:120]},
            )
            if not was_created and not card.vehicle and row["label"]:
                card.vehicle = row["label"][:120]
            card.ext_number = row["ext_number"][:64]
            card.external_id = row["external_id"][:32]
            card.status = row["status"]
            card.status_updated_at = now
            card.save()
            created += was_created
            updated += not was_created
    log.info("Petroline: картки оновлено, нових %s, оновлено %s", created, updated)
    return created, updated


# Читання карток із API провайдера (кнопки «Оновити …» на сторінці «Карти»)
SYNCERS = {
    Provider.UKRNAFTA.value: sync_ukrnafta_cards,
    Provider.PETROLINE.value: sync_petroline_cards,
}
SYNC_LABELS = {
    Provider.UKRNAFTA.value: "Оновити статуси UkrNafta",
    Provider.PETROLINE.value: "Оновити карти Petroline",
}


def sync_map() -> dict:
    """{code: True/False}: чи доступне оновлення карток провайдера (достатньо, щоб він був активний)."""
    configs = ProviderConfig.ensure_all()
    return {code: bool(cfg.is_active and code in SYNCERS) for code, cfg in configs.items()}


def sync_cards(provider: str) -> tuple[int, int]:
    """Оновлення карток обраного провайдера; повертає (нових, оновлено)."""
    if provider not in SYNCERS:
        raise ValueError("Для цього провайдера оновлення карток не підтримується")
    if not sync_map().get(provider):
        raise ValueError("Провайдер неактивний")
    return SYNCERS[provider]()


def set_status(card: Card, new_status: int) -> None:
    """Блокування/розблокування: спершу API, у базу пишемо лише після успіху."""
    if new_status not in (CardStatus.ACTIVE, CardStatus.BLOCKED):
        raise ValueError("Дозволено лише статуси «Активна» і «Заблокована»")
    if card.status not in (CardStatus.ACTIVE, CardStatus.BLOCKED):
        raise ValueError("Поточний статус невідомий або не дозволяє зміну. Оновіть статуси з UkrNafta")
    if not editable_map().get(card.provider):
        raise ValueError("Для цього провайдера режим редагування вимкнений")
    CLIENTS[card.provider]().update_card(card.number, status=new_status)
    card.status = new_status
    card.status_updated_at = timezone.now()
    card.save(update_fields=["status", "status_updated_at"])


def update_info(card: Card, vehicle: str, driver_name: str) -> None:
    """Авто/ПІБ: для провайдерів із режимом редагування спершу API, інакше лише локально."""
    vehicle, driver_name = vehicle.strip()[:120], " ".join(driver_name.split())[:120]
    if editable_map().get(card.provider):
        last, first = split_name(driver_name)
        kwargs = {"vin": vehicle}
        if driver_name:
            kwargs.update(last_name=last, first_name=first)
        CLIENTS[card.provider]().update_card(card.number, **kwargs)
    card.vehicle = vehicle
    card.driver_name = driver_name
    card.save(update_fields=["vehicle", "driver_name"])