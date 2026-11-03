import logging
from collections import Counter

from django.db import transaction
from django.utils import timezone

from .models import AuditLog, Card, CardStatus, Provider, ProviderConfig
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


def record_audit(
    action: str,
    card: Card,
    user=None,
    *,
    changes: dict | None = None,
    via_api: bool = False,
    error: str = "",
) -> AuditLog:
    """Запис у журнал аудиту + рядок у лог сервера."""
    username = user.get_username() if user is not None and user.is_authenticated else ""
    entry = AuditLog.objects.create(
        user=user if username else None,
        username=username,
        action=action,
        card=card if card.pk else None,
        card_label=str(card)[:200],
        provider=card.provider,
        changes=changes or {},
        via_api=via_api,
        success=not error,
        error=error,
    )
    log.info(
        "Аудит: %s %s картка %s:%s api=%s успіх=%s зміни=%s %s",
        username or "-",
        action,
        card.provider,
        card.number,
        via_api,
        not error,
        changes or {},
        error,
    )
    return entry


def _call_api(action: str, card: Card, user, changes: dict, call) -> None:
    """Виклик API провайдера; невдача теж потрапляє в журнал аудиту, потім помилка йде далі."""
    try:
        call()
    except Exception as exc:
        record_audit(action, card, user, changes=changes, via_api=True, error=str(exc)[:1000])
        raise


def set_status(card: Card, new_status: int, *, user=None) -> None:
    """Блокування/розблокування: спершу API, у базу пишемо лише після успіху."""
    if new_status not in (CardStatus.ACTIVE, CardStatus.BLOCKED):
        raise ValueError("Дозволено лише статуси «Активна» і «Заблокована»")
    if card.status not in (CardStatus.ACTIVE, CardStatus.BLOCKED):
        raise ValueError("Поточний статус невідомий або не дозволяє зміну. Оновіть статуси з UkrNafta")
    if not editable_map().get(card.provider):
        raise ValueError("Для цього провайдера режим редагування вимкнений")
    changes = {"status": [card.status, int(new_status)]}
    _call_api(
        AuditLog.Action.CARD_STATUS,
        card,
        user,
        changes,
        lambda: CLIENTS[card.provider]().update_card(card.number, status=new_status),
    )
    with transaction.atomic():
        card.status = new_status
        card.status_updated_at = timezone.now()
        card.save(update_fields=["status", "status_updated_at"])
        record_audit(AuditLog.Action.CARD_STATUS, card, user, changes=changes, via_api=True)


def update_info(card: Card, vehicle: str, driver_name: str, note: str | None = None, *, user=None) -> bool:
    """Авто/ПІБ/Примітка. Авто й ПІБ для провайдерів із режимом редагування спершу йдуть в API,
    інакше (і примітка завжди) — лише в базу. Повертає False, якщо змін немає.
    """
    new = {
        "vehicle": vehicle.strip()[:120],
        "driver_name": " ".join(driver_name.split())[:120],
    }
    if note is not None:
        new["note"] = note.strip()[:200]
    changes = {field: [getattr(card, field), value] for field, value in new.items() if getattr(card, field) != value}
    if not changes:
        return False

    api_changed = bool({"vehicle", "driver_name"} & changes.keys())
    via_api = api_changed and bool(editable_map().get(card.provider))
    if via_api:
        last, first = split_name(new["driver_name"])
        kwargs = {"vin": new["vehicle"]}
        if new["driver_name"]:
            kwargs.update(last_name=last, first_name=first)
        _call_api(
            AuditLog.Action.CARD_INFO,
            card,
            user,
            changes,
            lambda: CLIENTS[card.provider]().update_card(card.number, **kwargs),
        )
    with transaction.atomic():
        for field, value in new.items():
            setattr(card, field, value)
        card.save(update_fields=list(new))
        record_audit(AuditLog.Action.CARD_INFO, card, user, changes=changes, via_api=via_api)
    return True


# Пояснення, чому статус не можна змінити, для провайдерів без керування через API
_NO_API_REASONS = {
    Provider.PETROLINE.value: "API карток Petroline зараз лише читає: блокування не підтримується.",
    Provider.UPG.value: "Для UPG немає API карток: зміни зберігаються лише в нашій базі.",
}


def capabilities(card: Card) -> dict:
    """Що можна робити з карткою на її сторінці і чому ні."""
    api_edit = bool(editable_map().get(card.provider))
    status_known = card.status in (CardStatus.ACTIVE, CardStatus.BLOCKED)
    if api_edit and not status_known:
        status_hint = "Статус невідомий або не дозволяє зміну. Оновіть статуси з UkrNafta на сторінці «Карти»."
    elif not api_edit and card.provider in CLIENTS:
        status_hint = "Редагування через API вимкнене в «Конфігурації провайдера» або провайдер неактивний."
    elif not api_edit:
        status_hint = _NO_API_REASONS.get(card.provider, "Керування карткою через API не підтримується.")
    else:
        status_hint = ""
    return {
        "api_edit": api_edit,
        "can_toggle_status": api_edit and status_known,
        "next_status": CardStatus.ACTIVE if card.status == CardStatus.BLOCKED else CardStatus.BLOCKED,
        "status_hint": status_hint,
        # У провайдера з API, але з вимкненим редагуванням, зміни розійдуться з даними провайдера
        "local_only_warning": card.provider in CLIENTS and not api_edit,
    }
