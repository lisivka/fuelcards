"""Клієнт карток Petroline (apiv2.petroline.in.ua): список карток, зміна статусу, Авто/ПІБ.

Авто/ПІБ у Petroline — не поля картки, а ім'я водія, до якого вона прив'язана (рішення 004):
- «Персональна» (тип 1) з водієм -> перейменовуємо наявного водія: «Авто | ПІБ» (так пишуть менеджери);
- «Техпролив» (тип 3) з водієм -> перейменовуємо водія: лише ПІБ;
- «Автомобільна» (тип 5) або картка без водія -> картка стає «Персональною»: прив'язуємо її до
  наявного водія з таким самим ім'ям (наприклад, водій отримав нову картку замість загубленої),
  а якщо такого немає — створюємо водія;
- інші типи і картки, тип яких ще не оновлено з Petroline, — не змінюються.
Petroline показує водія як «прізвище ім'я по батькові», тому весь текст пишемо в прізвище.
"""

from dataclasses import dataclass

from ..models import CardStatus
from .petroline_api import PetrolineClient, PetrolineError

__all__ = ["PetrolineCards", "PetrolineError", "format_label", "parse_label"]

# Статуси Petroline -> наші (CardStatus). Невідомі коди не вгадуємо: None.
STATUS_FROM_API = {3: CardStatus.ACTIVE.value, 5: CardStatus.BLOCKED.value}
STATUS_TO_API = {v: k for k, v in STATUS_FROM_API.items()}
STATUS_ACTIVE_API = 3

TYPE_PERSONAL = 1  # «Персональна»: поле Водій
TYPE_MOVE = 2  # «Переміщення»
TYPE_TECH = 3  # «Техпролив»: поле Водій
TYPE_VEHICLE = 5  # «Автомобільна»: поле Транспорт
TYPE_NAMES = {
    TYPE_PERSONAL: "Персональна",
    TYPE_MOVE: "Переміщення",
    TYPE_TECH: "Техпролив",
    TYPE_VEHICLE: "Автомобільна",
}
FUEL_DIESEL = 1  # пальне для нового водія, якого створюємо ми (лише ДП)
SEPARATOR = " | "

# Що робить збереження Авто/ПІБ у Petroline залежно від картки
EDIT_RENAME = "rename"  # перейменувати водія на «Авто | ПІБ»
EDIT_DRIVER = "driver"  # перейменувати водія: лише ПІБ («Техпролив»)
EDIT_LINK = "link"  # зробити «Персональною»: прив'язати до водія «Авто | ПІБ» (наявного або нового)
EDIT_NONE = "none"  # у Petroline не змінюється

# Поля KeyDto для PUT /api/Keys/update: API приймає лише повний запис картки
KEY_DTO_FIELDS = (
    "id",
    "groupId",
    "personId",
    "transportId",
    "status",
    "typeId",
    "number",
    "barrelId",
    "extNumber",
    "pin",
    "comment",
    "secondCardMode",
    "secondCardId",
)
PERSON_DTO_FIELDS = ("id", "groupId", "email", "phone", "fuels", "status")


def _all_by_id(skip: int, take: int) -> dict:
    return {"orderBy": [{"field": "id", "order": 0}], "skip": skip, "take": take}


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def _name_key(text) -> str:
    """Для пошуку водія за ім'ям: без зайвих пробілів і без різниці у великих/малих літерах."""
    return _clean(text).casefold()


def format_label(vehicle: str, driver_name: str) -> str:
    """«Авто | ПІБ»; якщо одне з полів порожнє — лише інше, без роздільника."""
    return SEPARATOR.join(part for part in (_clean(vehicle), _clean(driver_name)) if part)


def parse_label(text: str) -> tuple[str, str]:
    """«Авто | ПІБ» -> (Авто, ПІБ). Без «|» увесь текст — Авто: старі імена переважно «Авто_ПІБ»."""
    vehicle, sep, driver = str(text or "").partition("|")
    return (_clean(vehicle), _clean(driver)) if sep else (_clean(text), "")


def edit_mode(card_type: int | None, person_id: str) -> str:
    """Що робить збереження Авто/ПІБ у Petroline (див. EDIT_*)."""
    if card_type == TYPE_PERSONAL:
        return EDIT_RENAME if person_id else EDIT_LINK
    if card_type == TYPE_TECH and person_id:
        return EDIT_DRIVER
    if card_type == TYPE_VEHICLE:
        return EDIT_LINK
    return EDIT_NONE


def label_fields(card_type: int | None, person_id: str, label: str) -> tuple[str, str]:
    """Як ім'я з Petroline лягає в наші (Авто, ПІБ)."""
    if not person_id:  # транспорт («Модель Номер») або нічого
        return _clean(label), ""
    if card_type == TYPE_TECH:
        return "", _clean(label)
    return parse_label(label)


def expected_label(card_type: int | None, person_id: str, vehicle: str, driver_name: str) -> str:
    """Як мало б бути записано в Petroline за нашими Авто/ПІБ."""
    if edit_mode(card_type, person_id) == EDIT_DRIVER:
        return _clean(driver_name)
    return format_label(vehicle, driver_name)


@dataclass(frozen=True)
class InfoResult:
    """Що записано в Petroline і як картка прив'язана після запису."""

    card_type: int
    person_id: str
    transport_id: str
    label: str  # як Petroline тепер показує водія
    action: str = "renamed"  # renamed | linked (до наявного водія) | created (новий водій)


class PetrolineCards:
    """Картки Petroline: список, блокування/розблокування і запис Авто/ПІБ."""

    def __init__(self, client: PetrolineClient | None = None):
        self.client = client or PetrolineClient()

    # --- читання -------------------------------------------------------------------------------

    def find_card(self, card_number: str) -> dict:
        """Повний запис картки з Keys/list за Ідентифікатором (`number`)."""
        where = {
            "operatorType": 1,
            "operands": [
                {"operatorType": 0, "operands": None, "field": "number", "filterType": 1, "value": card_number}
            ],
            "field": None,
            "filterType": 0,
            "value": None,
        }
        body = {"where": where, "orderBy": [{"field": "id", "order": 0}], "skip": 0, "take": 5}
        rows = self.client.post("/api/Keys/list", body)
        found = [r for r in rows if str(r.get("number")) == str(card_number)] if isinstance(rows, list) else []
        if len(found) != 1:
            raise PetrolineError(f"Картку {card_number} у Petroline не знайдено (збігів: {len(found)})")
        return found[0]

    def list_persons(self) -> list[dict]:
        return self.client.post_paged("/api/Persons/list", _all_by_id)

    def list_cards(self) -> list[dict]:
        records = self.client.post_paged("/api/Keys/list", _all_by_id)
        return [row for row in map(self._normalize, records) if row]

    @staticmethod
    def _normalize(rec: dict) -> dict | None:
        """Лише потрібні поля; PIN та інше чутливе далі не передається."""
        number = str(rec.get("number") or "").strip()
        if not number:
            return None
        card_type = rec.get("typeId")
        person_id = str(rec.get("personId") or "")
        if person_id:
            label = _clean(rec.get("personName"))
        else:
            label = _clean(f"{rec.get('transportName') or ''} {rec.get('transportNumber') or ''}")
        vehicle, driver = label_fields(card_type, person_id, label)
        return {
            "number": number,
            "ext_number": str(rec.get("extNumber") or "").strip(),
            "external_id": str(rec.get("id") or ""),
            "api_status": rec.get("status"),
            "status": STATUS_FROM_API.get(rec.get("status")),
            "card_type": card_type,
            "person_id": person_id,
            "transport_id": str(rec.get("transportId") or ""),
            "label": label,
            "vehicle": vehicle,
            "driver_name": driver,
        }

    # --- запис ---------------------------------------------------------------------------------

    def update_card(self, card_number: str, *, status: int) -> None:
        """Зміна статусу: читаємо повний запис картки, міняємо лише `status`, решту надсилаємо як є.

        PIN-кодів на картках не використовуємо: у Keys/list `pin` порожній, і так він і йде назад.
        Перевірено на живому API (2026-11): інші поля після запису не змінюються.
        """
        if status not in STATUS_TO_API:
            raise PetrolineError("Petroline: дозволено лише статуси «Активна» і «Заблокована»")
        record = self.find_card(card_number)
        body = {field: record.get(field) for field in KEY_DTO_FIELDS}
        body["status"] = STATUS_TO_API[status]
        self.client.put("/api/Keys/update", body)

    def update_info(
        self,
        card_number: str,
        *,
        vehicle: str = "",
        driver_name: str = "",
        expected: tuple[int | None, str] | None = None,
    ) -> InfoResult:
        """Пише Авто/ПІБ у Petroline за свіжим записом картки.

        `expected` — (тип, id водія) з нашого останнього «Оновити карти»: якщо в Petroline вже
        інакше, нічого не пишемо — користувач бачив на сторінці не те, що є насправді.
        """
        card = self.find_card(card_number)
        card_type = card.get("typeId")
        person_id = str(card.get("personId") or "")
        if expected is not None and (expected[0], str(expected[1] or "")) != (card_type, person_id):
            raise PetrolineError(
                "картка в Petroline змінилась після останнього оновлення (тип або водій) — "
                "натисніть «Оновити карти Petroline» і перевірте дані"
            )
        mode = edit_mode(card_type, person_id)
        if mode == EDIT_NONE:
            raise PetrolineError(f"Petroline: картку типу «{TYPE_NAMES.get(card_type, card_type)}» тут не змінюємо")
        text = _clean(driver_name) if mode == EDIT_DRIVER else format_label(vehicle, driver_name)
        if not text:
            raise PetrolineError("Petroline: ім'я водія не може бути порожнім")

        persons = self.list_persons()
        same = [p for p in persons if _name_key(p.get("name")) == _name_key(text)]
        if mode == EDIT_LINK:
            return self._link(card, text, same)
        others = [p for p in same if str(p.get("id")) != person_id]
        if others:
            ids = ", ".join(str(p.get("id")) for p in others)
            raise PetrolineError(f"у Petroline вже є інший водій «{text}» (id {ids}) — ім'я водія має бути унікальним")
        person = next((p for p in persons if str(p.get("id")) == person_id), None)
        if person is None:
            raise PetrolineError(f"Petroline: водія {person_id} не знайдено")
        body = {field: person.get(field) for field in PERSON_DTO_FIELDS}
        body.update({"surname": text, "name": "", "middlename": ""})
        self.client.write("put", "/api/Persons/update", body)
        return InfoResult(card_type, person_id, str(card.get("transportId") or ""), text)

    def _link(self, card: dict, text: str, same: list[dict]) -> InfoResult:
        """Картка стає «Персональною» на водієві `text`: наявному (якщо він один і активний) або новому."""
        if len(same) > 1:
            ids = ", ".join(str(p.get("id")) for p in same)
            raise PetrolineError(f"у Petroline кілька водіїв «{text}» (id {ids}) — прив'яжіть картку в кабінеті")
        if not same:
            return self._create_person_and_link(card, text)
        person = same[0]
        if person.get("status") != STATUS_ACTIVE_API:
            raise PetrolineError(
                f"водій «{text}» (id {person.get('id')}) у Petroline неактивний — активуйте його в кабінеті"
            )
        person_id = int(person["id"])
        self._set_person(card, person_id)
        return InfoResult(TYPE_PERSONAL, str(person_id), "", text, action="linked")

    def _set_person(self, card: dict, person_id: int) -> None:
        """Keys/update: картка «Персональна» на водієві person_id, без транспорту."""
        body = {field: card.get(field) for field in KEY_DTO_FIELDS}
        body.update({"typeId": TYPE_PERSONAL, "personId": person_id, "transportId": None})
        self.client.put("/api/Keys/update", body)

    def _create_person_and_link(self, card: dict, text: str) -> InfoResult:
        """Новий водій «Авто | ПІБ» + картка стає «Персональною». Якщо прив'язка не вдалась — водія видаляємо."""
        created = self.client.write(
            "post",
            "/api/Persons/create",
            {
                "surname": text,
                "name": "",
                "middlename": "",
                "groupId": card.get("groupId"),
                "email": None,
                "phone": None,
                "fuels": [FUEL_DIESEL],
                "status": STATUS_ACTIVE_API,
            },
        )
        raw_id = created.get("value") if isinstance(created, dict) else created
        try:
            person_id = int(str(raw_id).strip().strip('"'))
        except (TypeError, ValueError):
            raise PetrolineError(f"Petroline Persons/create: неочікувана відповідь {created!r}") from None
        try:
            self._set_person(card, person_id)
        except Exception:
            try:
                self.client.write("delete", f"/api/Persons/delete/{person_id}")
            except Exception as cleanup_exc:  # noqa: BLE001 — головна помилка важливіша
                raise PetrolineError(
                    f"Petroline: картку не прив'язано, і створеного водія {person_id} не вдалося видалити "
                    f"({cleanup_exc}) — видаліть його в кабінеті"
                ) from None
            raise
        return InfoResult(TYPE_PERSONAL, str(person_id), "", text, action="created")
