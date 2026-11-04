import logging
from collections import Counter

from django.db import transaction
from django.db.models import Count, Max
from django.utils import timezone

from .audit import record_sync, username_of
from .models import AuditLog, Card, CardStatus, Provider, ProviderConfig
from .providers import petroline_cards as pl
from .providers.petroline_cards import PetrolineCards
from .providers.ukrnafta_cards import UkrnaftaCards

log = logging.getLogger(__name__)

# Провайдери, для яких реалізоване керування картками через API (блокування/розблокування)
CLIENTS = {Provider.UKRNAFTA.value: UkrnaftaCards, Provider.PETROLINE.value: PetrolineCards}
# Провайдери, у яких Авто/ПІБ теж змінюються через API. У Petroline — через водія або транспорт,
# до яких прив'язана картка (див. fuel/providers/petroline_cards.py, рішення 004)
INFO_API_PROVIDERS = {Provider.UKRNAFTA.value, Provider.PETROLINE.value}


def info_via_api(provider: str, editable: dict | None = None) -> bool:
    """Чи змінюються Авто/ПІБ картки у провайдера (а не лише в нашій базі)."""
    editable = editable if editable is not None else editable_map()
    return provider in INFO_API_PROVIDERS and bool(editable.get(provider))


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
        # Petroline: тип картки і чи розходяться наші Авто/ПІБ з Petroline (за останнім оновленням)
        "pl_type": pl.TYPE_NAMES.get(card.provider_card_type, "") if card.provider == Provider.PETROLINE.value else "",
        "pl_mismatch": petroline_mismatch(card),
        "pl_label": card.provider_label,
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
    """Підтягує картки Petroline: номер на картці, id, статус, тип і прив'язку (водій/транспорт).

    Ключ картки: Ідентифікатор (`number`). Авто/ПІБ заповнюються з Petroline лише тоді, коли в нас
    обидва порожні («Авто | ПІБ» розділяється по «|», ім'я без «|» — в Авто); інакше не чіпаємо:
    розбіжність видно на сторінці картки, рішення за користувачем. Як картка записана в Petroline
    зараз — у `provider_label`.
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
            )
            if not card.vehicle and not card.driver_name:
                card.vehicle = row["vehicle"][:120]
                card.driver_name = row["driver_name"][:120]
            card.provider_card_type = row["card_type"]
            card.provider_person_id = row["person_id"][:32]
            card.provider_transport_id = row["transport_id"][:32]
            card.provider_label = row["label"][:200]
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


def sync_cards(provider: str, *, user=None, source: str = AuditLog.SOURCE_BUTTON) -> tuple[int, int]:
    """Оновлення карток обраного провайдера; повертає (нових, оновлено). Результат — у журнал аудиту."""
    if provider not in SYNCERS:
        raise ValueError("Для цього провайдера оновлення карток не підтримується")
    if not sync_map().get(provider):
        raise ValueError("Провайдер неактивний")
    try:
        created, updated = SYNCERS[provider]()
    except Exception as exc:
        record_sync(AuditLog.Action.CARDS_SYNC, provider, user=user, source=source, error=str(exc))
        raise
    record_sync(
        AuditLog.Action.CARDS_SYNC, provider, user=user, source=source, created=created, updated=updated
    )
    return created, updated


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
    username = username_of(user)
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


def _call_api(action: str, card: Card, user, changes: dict, call):
    """Виклик API провайдера; невдача теж потрапляє в журнал аудиту, потім помилка йде далі."""
    try:
        return call()
    except Exception as exc:
        record_audit(action, card, user, changes=changes, via_api=True, error=str(exc)[:1000])
        raise


def set_status(card: Card, new_status: int, *, user=None) -> None:
    """Блокування/розблокування: спершу API, у базу пишемо лише після успіху."""
    if new_status not in (CardStatus.ACTIVE, CardStatus.BLOCKED):
        raise ValueError("Дозволено лише статуси «Активна» і «Заблокована»")
    if card.status not in (CardStatus.ACTIVE, CardStatus.BLOCKED):
        raise ValueError("Поточний статус невідомий або не дозволяє зміну. Оновіть картки з API провайдера")
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


def update_info(
    card: Card,
    vehicle: str,
    driver_name: str,
    note: str | None = None,
    *,
    user=None,
    push: bool = False,
) -> bool:
    """Авто/ПІБ/Примітка. Авто й ПІБ для провайдерів із режимом редагування спершу йдуть в API,
    інакше (і примітка завжди) — лише в базу. Повертає False, якщо змін немає.
    Для Petroline `push=True` — записати наші Авто/ПІБ у Petroline, навіть якщо в нас вони не змінились.
    """
    if card.provider == Provider.PETROLINE.value and info_via_api(card.provider):
        return _update_petroline_info(card, vehicle, driver_name, note, user=user, push=push)
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
    via_api = api_changed and info_via_api(card.provider)
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


def _clean(text: str | None) -> str:
    return " ".join(str(text or "").split())


def petroline_edit_mode(card: Card) -> str:
    """Що робить збереження Авто/ПІБ у Petroline (pl.EDIT_*), за даними останнього «Оновити карти»."""
    return pl.edit_mode(card.provider_card_type, card.provider_person_id)


def petroline_expected_label(card: Card, vehicle: str | None = None, driver_name: str | None = None) -> str:
    """Як картка мала б бути записана в Petroline за нашими (або переданими) Авто/ПІБ."""
    return pl.expected_label(
        card.provider_card_type,
        card.provider_person_id,
        card.vehicle if vehicle is None else vehicle,
        card.driver_name if driver_name is None else driver_name,
    )


def petroline_mismatch(card: Card) -> bool:
    """Наші Авто/ПІБ не збігаються з тим, як картка записана в Petroline (за останнім оновленням карток)."""
    if card.provider != Provider.PETROLINE.value or card.provider_card_type is None:
        return False
    return _clean(card.provider_label) != petroline_expected_label(card)


def shared_person_cards(card: Card) -> list[Card]:
    """Інші наші картки Petroline, прив'язані до того самого водія (зміна імені водія зачепить і їх)."""
    if card.provider != Provider.PETROLINE.value or not card.provider_person_id:
        return []
    return list(
        Card.objects.filter(provider=card.provider, provider_person_id=card.provider_person_id)
        .exclude(pk=card.pk)
        .annotate(last_tx=Max("transactions__timestamp"))
        .order_by("status", "number")
    )


def same_name_cards(card: Card, label: str) -> list[Card]:
    """Наші картки Petroline, водій яких записаний як `label` (за останнім оновленням карток).

    Порівняння без різниці у великих/малих літерах — так само, як шукає водія запис у Petroline.
    """
    key = _clean(label).casefold()
    if not key:
        return []
    cards = Card.objects.filter(provider=Provider.PETROLINE.value).exclude(provider_person_id="").exclude(pk=card.pk)
    return [c for c in cards.order_by("status", "number") if _clean(c.provider_label).casefold() == key]


def person_duplicates() -> list[dict]:
    """Водії Petroline, на яких записано 2+ активні картки: [{"person_id", "label", "cards"}]."""
    person_ids = list(
        Card.objects.filter(provider=Provider.PETROLINE.value, status=CardStatus.ACTIVE)
        .exclude(provider_person_id="")
        .values("provider_person_id")
        .annotate(n=Count("id"))
        .filter(n__gt=1)
        .values_list("provider_person_id", flat=True)
    )
    groups: dict[str, list[Card]] = {}
    cards = Card.objects.filter(provider=Provider.PETROLINE.value, provider_person_id__in=person_ids)
    for c in cards.annotate(last_tx=Max("transactions__timestamp")).order_by("provider_person_id", "status", "number"):
        groups.setdefault(c.provider_person_id, []).append(c)
    return [
        {"person_id": pid, "label": next((c.provider_label for c in group if c.provider_label), ""), "cards": group}
        for pid, group in groups.items()
    ]


_REFRESH_PETROLINE = "натисніть «Оновити карти Petroline» на сторінці «Карти»"


def _petroline_new_values(card: Card, mode: str, vehicle: str, driver_name: str) -> dict:
    """Нові Авто/ПІБ: у «Техпроливі» Авто не редагується (форма його не надсилає) і лишається як було."""
    return {
        "vehicle": card.vehicle if mode == pl.EDIT_DRIVER else _clean(vehicle)[:120],
        "driver_name": _clean(driver_name)[:120],
    }


def _update_petroline_info(card: Card, vehicle: str, driver_name: str, note: str | None, *, user, push: bool) -> bool:
    """Авто/ПІБ картки Petroline.

    - «Персональна»/«Техпролив»: збереження перейменовує водія в Petroline (якщо ім'я там інше),
      потім ця картка й картки того самого водія в нашій базі — кожна з записом у журналі.
    - «Автомобільна» і картка без водія: збереження — лише в нашій базі; у Petroline — тільки
      кнопкою «Записати наше в Petroline» (push): картка стає «Персональною» на наявному водієві
      з таким ім'ям або на новому.
    """
    mode = petroline_edit_mode(card)
    new = _petroline_new_values(card, mode, vehicle, driver_name)
    if note is not None:
        new["note"] = note.strip()[:200]
    changes = {f: [getattr(card, f), v] for f, v in new.items() if getattr(card, f) != v}
    info_changed = bool({"vehicle", "driver_name"} & changes.keys())
    if mode == pl.EDIT_NONE and (info_changed or push):
        if card.provider_card_type is None:
            raise ValueError(f"Тип картки в Petroline ще невідомий: {_REFRESH_PETROLINE}")
        type_name = pl.TYPE_NAMES.get(card.provider_card_type, card.provider_card_type)
        raise ValueError(f"Авто/ПІБ картки типу «{type_name}» через сайт не змінюються")

    old_label = _clean(card.provider_label)
    label = petroline_expected_label(card, new["vehicle"], new["driver_name"])
    if mode == pl.EDIT_LINK:
        to_api = push
    else:
        to_api = (push or info_changed) and label != old_label
    if not changes and not to_api:
        return False
    if to_api and not label:
        raise ValueError("Petroline не приймає порожнє ім'я водія: вкажіть Авто або ПІБ")

    result = None
    if to_api:
        changes.setdefault("provider_label", [old_label, label])
        result = _call_api(
            AuditLog.Action.CARD_INFO,
            card,
            user,
            changes,
            lambda: PetrolineCards().update_info(
                card.number,
                vehicle=new["vehicle"],
                driver_name=new["driver_name"],
                expected=(card.provider_card_type, card.provider_person_id),
            ),
        )
    shared = shared_person_cards(card) if result and result.action == "renamed" else []
    with transaction.atomic():
        for field, value in new.items():
            setattr(card, field, value)
        fields = list(new)
        if result:
            card.provider_card_type = result.card_type
            card.provider_person_id = result.person_id
            card.provider_transport_id = result.transport_id
            card.provider_label = result.label[:200]
            fields += ["provider_card_type", "provider_person_id", "provider_transport_id", "provider_label"]
            if result.action == "created":
                changes["provider_label"][1] += f" — новий водій у Petroline (id {result.person_id}), тип «Персональна»"
            elif result.action == "linked":
                changes["provider_label"][1] += (
                    f" — прив'язано до наявного водія Petroline (id {result.person_id}), тип «Персональна»"
                )
        card.save(update_fields=fields)
        record_audit(AuditLog.Action.CARD_INFO, card, user, changes=changes, via_api=bool(result))
        for other in shared:
            _apply_shared(other, card, new, result.label, user)
    return True


def _apply_shared(other: Card, source: Card, new: dict, label: str, user) -> None:
    """Картка того самого водія Petroline отримує ті самі Авто/ПІБ (у Petroline це один запис)."""
    other_changes = {f: [getattr(other, f), new[f]] for f in ("vehicle", "driver_name") if getattr(other, f) != new[f]}
    other_changes["provider_label"] = [other.provider_label, f"{label} (спільний водій з карткою {source.display_number})"]
    other.vehicle, other.driver_name, other.provider_label = new["vehicle"], new["driver_name"], label[:200]
    other.save(update_fields=["vehicle", "driver_name", "provider_label"])
    record_audit(AuditLog.Action.CARD_INFO, other, user, changes=other_changes, via_api=True)


def take_from_petroline(card: Card, *, user=None) -> bool:
    """«Взяти з Petroline»: наші Авто/ПІБ = як картка записана в Petroline (останнє оновлення). Лише база."""
    if card.provider != Provider.PETROLINE.value or card.provider_card_type is None:
        raise ValueError(f"Даних Petroline для цієї картки ще немає: {_REFRESH_PETROLINE}")
    vehicle, driver = pl.label_fields(card.provider_card_type, card.provider_person_id, card.provider_label)
    new = {"vehicle": vehicle[:120], "driver_name": driver[:120]}
    changes = {f: [getattr(card, f), v] for f, v in new.items() if getattr(card, f) != v}
    if not changes:
        return False
    with transaction.atomic():
        card.vehicle, card.driver_name = new["vehicle"], new["driver_name"]
        card.save(update_fields=list(new))
        record_audit(AuditLog.Action.CARD_INFO, card, user, changes=changes)
    return True


# Пояснення, чому статус не можна змінити, для провайдерів без керування через API
_NO_API_REASONS = {
    Provider.UPG.value: "Для UPG немає API карток: зміни зберігаються лише в нашій базі.",
}
# Кнопка оновлення карток на сторінці «Карти» — підказка, де взяти свіжий статус
_REFRESH_HINTS = {
    Provider.UKRNAFTA.value: "Оновіть статуси з UkrNafta на сторінці «Карти».",
    Provider.PETROLINE.value: "Оновіть карти Petroline на сторінці «Карти».",
}


def capabilities(card: Card) -> dict:
    """Що можна робити з карткою на її сторінці і чому ні."""
    editable = editable_map()
    api_edit = bool(editable.get(card.provider))
    status_known = card.status in (CardStatus.ACTIVE, CardStatus.BLOCKED)
    if api_edit and not status_known:
        status_hint = "Статус невідомий або не дозволяє зміну. " + _REFRESH_HINTS.get(card.provider, "")
    elif not api_edit and card.provider in CLIENTS:
        status_hint = "Редагування через API вимкнене в «Конфігурації провайдера» або провайдер неактивний."
    elif not api_edit:
        status_hint = _NO_API_REASONS.get(card.provider, "Керування карткою через API не підтримується.")
    else:
        status_hint = ""
    caps = {
        "api_edit": api_edit,
        # Авто/ПІБ змінюються у провайдера (UkrNafta — поля картки; Petroline — ім'я водія)
        "info_api": info_via_api(card.provider, editable),
        "petroline": card.provider == Provider.PETROLINE.value,
        "can_toggle_status": api_edit and status_known,
        "next_status": CardStatus.ACTIVE if card.status == CardStatus.BLOCKED else CardStatus.BLOCKED,
        "status_hint": status_hint,
        # У провайдера з API, але з вимкненим редагуванням, зміни розійдуться з даними провайдера
        "local_only_warning": card.provider in INFO_API_PROVIDERS and not api_edit,
    }
    if caps["petroline"]:
        caps.update(_petroline_caps(card))
    return caps


def _petroline_caps(card: Card) -> dict:
    """Дані Petroline для сторінки картки: тип, водій, звірка, спільні картки (за останнім оновленням)."""
    mode = petroline_edit_mode(card)
    shared = shared_person_cards(card)
    expected = petroline_expected_label(card)
    same_name = same_name_cards(card, expected) if mode == pl.EDIT_LINK else []
    return {
        "edit_mode": mode,
        "type_known": card.provider_card_type is not None,
        "type_name": pl.TYPE_NAMES.get(card.provider_card_type, card.provider_card_type),
        "mode_rename": mode in (pl.EDIT_RENAME, pl.EDIT_DRIVER),
        "mode_link": mode == pl.EDIT_LINK,
        "mode_none": mode == pl.EDIT_NONE,
        "edit_vehicle": mode in (pl.EDIT_RENAME, pl.EDIT_LINK),
        "provider_label": _clean(card.provider_label),
        "expected_label": expected,
        "label_mismatch": petroline_mismatch(card),
        "from_petroline": pl.label_fields(card.provider_card_type, card.provider_person_id, card.provider_label),
        # Картка стане «Персональною»: чи є вже водій з таким ім'ям (тоді прив'яжемо до нього)
        "same_name_cards": same_name,
        "push_confirm": _push_confirm(card, mode, expected, shared, same_name),
        "shared_cards": shared,
        "shared_active": sum(1 for c in shared if c.status == CardStatus.ACTIVE),
    }


def _push_confirm(card: Card, mode: str, expected: str, shared: list[Card], same_name: list[Card]) -> str:
    """Текст підтвердження кнопки «Записати наше в Petroline»: що саме зміниться у провайдера."""
    if mode == pl.EDIT_LINK:
        text = f"Картка стане «Персональною» в Petroline на водієві «{expected}». "
        if same_name:
            numbers = ", ".join(c.display_number for c in same_name)
            text += f"Такий водій уже є (картки {numbers}) — картку буде прив'язано до нього."
        else:
            text += "Якщо такого водія немає — його буде створено (пальне ДП)."
    else:
        text = f"Petroline: ім'я водія «{_clean(card.provider_label)}» → «{expected}»."
        if shared:
            text += " Водій спільний: ім'я зміниться також для карток " + ", ".join(c.display_number for c in shared) + "."
    return text + " Продовжити?"
