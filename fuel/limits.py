"""Ліміти карток на пальне: перетворення відповідей провайдерів у єдиний вигляд і скорочення.

Ліміт у нашій базі (`Card.fuel_limits`) — список словників:
    {"period": "Д", "money": False, "amount": 500.0, "fuels": ["ДП", "ДП ENERGY"]}
Скорочення: літера періоду + значення, гривні позначаються «₴»: «Д500 М500», «Т100₴».
Знімок оновлюється кнопкою оновлення карток (і після розблокування картки), не наживо.

UkrNafta (перевірка 2026-11-04): ліміт на картку, `limitType` 1–10, види пального — «гаманці»
(`attachedPurses`, довідник `GET /common/purses` з ознакою `isFuel`).
Petroline: ліміт на водія (`PersonLimit`, рішення 004), один запис на вид пального; значення в літрах;
діючі — `limitStatus` 1 (завершені мають 3 і дату завершення).
"""

import logging
from collections.abc import Iterable

log = logging.getLogger(__name__)

# Порядок періодів у скороченні і пояснення для підказки
PERIODS = {
    "Р": "одноразово (за одну заправку)",
    "Д": "на добу",
    "Т": "на тиждень",
    "М": "на місяць",
    "С": "до списання (загальний, не відновлюється)",
}
MONEY_SIGN = "₴"
HELP_TEXT = (
    "Ліміти на пальне: літера — період, число — значення ліміту. "
    + "; ".join(f"{code} — {text}" for code, text in PERIODS.items())
    + f". Без позначки — літри, «{MONEY_SIGN}» — гривні. Наприклад, Д500 М500 — 500 л на добу і 500 л на місяць."
    " Дані на момент останнього оновлення карток."
)

# UkrNafta: limitType -> (період, у гривнях)
UKRNAFTA_TYPES = {
    1: ("М", True),
    2: ("Д", True),
    3: ("М", False),
    4: ("Д", False),
    5: ("Т", True),
    6: ("Т", False),
    7: ("Р", True),
    8: ("Р", False),
    9: ("С", True),
    10: ("С", False),
}

# Petroline: види пального (LibFuelType), які не є пальним — ліміти на них не показуємо
PETROLINE_NON_FUEL = {8, 15, 16, 17}  # AdBlue, Омивач, Олива, Вода
PETROLINE_LIMIT_ACTIVE = 1  # limitStatus діючого ліміту
# Поле PersonLimit -> період. totalLimit не трапився жодного разу; якщо з'явиться — показуємо як «С»
PETROLINE_FIELDS = (("onceLimit", "Р"), ("dayLimit", "Д"), ("totalLimit", "С"))


def _number(value) -> float | None:
    """Значення ліміту; 0, від'ємні й порожні не показуємо (свідомо: у даних 2026-11 таких немає, а що
    означає 0 у провайдера — «заборонено» чи «не задано» — невідомо; з'ясувати в ЗАДАЧІ-4б)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _merge(limits: Iterable[dict]) -> list[dict]:
    """Однакові (період, гроші, значення) з різними видами пального — в один запис; сортування за періодом."""
    merged: dict[tuple, dict] = {}
    for item in limits:
        key = (item["period"], item["money"], item["amount"])
        if key in merged:
            merged[key]["fuels"] = sorted(set(merged[key]["fuels"]) | set(item["fuels"]))
        else:
            merged[key] = {**item, "fuels": sorted(set(item["fuels"]))}
    order = list(PERIODS)
    return sorted(merged.values(), key=lambda x: (order.index(x["period"]), x["money"], x["amount"]))


def ukrnafta_limits(raw: list[dict], fuel_purses: dict[int, str]) -> list[dict]:
    """Ліміти картки UkrNafta -> наш вигляд. Лише ліміти, прив'язані хоча б до одного гаманця пального."""
    result = []
    for rec in raw or []:
        kind = UKRNAFTA_TYPES.get(rec.get("limitType"))
        amount = _number(rec.get("limitAmount"))
        if kind is None or amount is None:
            if kind is None:
                log.warning("UkrNafta: невідомий тип ліміту %s", rec.get("limitType"))
            continue
        purse_ids = [p.get("purseId") for p in rec.get("attachedPurses") or []]
        fuels = [fuel_purses[pid] for pid in purse_ids if pid in fuel_purses]
        if not fuels:
            continue
        result.append({"period": kind[0], "money": kind[1], "amount": amount, "fuels": fuels})
    return _merge(result)


def petroline_limits(records: list[dict]) -> list[dict]:
    """Діючі ліміти одного водія Petroline -> наш вигляд (лише пальне, значення в літрах)."""
    result = []
    for rec in records or []:
        if rec.get("limitStatus") != PETROLINE_LIMIT_ACTIVE or rec.get("fuelId") in PETROLINE_NON_FUEL:
            continue
        fuel = " ".join(str(rec.get("fuel") or "").split()) or f"пальне {rec.get('fuelId')}"
        for field, period in PETROLINE_FIELDS:
            amount = _number(rec.get(field))
            if amount is not None:
                result.append({"period": period, "money": False, "amount": amount, "fuels": [fuel]})
    return _merge(result)


def petroline_by_person(records: list[dict]) -> dict[str, list[dict]]:
    """PersonLimit/list -> {personId (рядок): ліміти на пальне}."""
    grouped: dict[str, list[dict]] = {}
    for rec in records or []:
        if rec.get("personId"):
            grouped.setdefault(str(rec["personId"]), []).append(rec)
    return {person: petroline_limits(recs) for person, recs in grouped.items()}


def _fmt_amount(amount: float) -> str:
    return str(int(amount)) if float(amount).is_integer() else f"{amount:g}"


def short_one(item: dict) -> str:
    return f"{item['period']}{_fmt_amount(item['amount'])}{MONEY_SIGN if item['money'] else ''}"


def short(limits: list[dict] | None) -> str:
    """«Д500 М500»; однакове скорочення для різних видів пального — один раз."""
    parts: list[str] = []
    for item in limits or []:
        text = short_one(item)
        if text not in parts:
            parts.append(text)
    return " ".join(parts)


def describe(limits: list[dict] | None) -> list[str]:
    """Для сторінки картки: «Д500 — на добу: ДП, ДП ENERGY»."""
    return [
        f"{short_one(item)} — {PERIODS[item['period']]}"
        f"{', гривні' if item['money'] else ', літри'}: {', '.join(item['fuels'])}"
        for item in limits or []
    ]
