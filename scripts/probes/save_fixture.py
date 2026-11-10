"""Знеособлені фікстури транзакцій UPG і UkrNafta для тестів (ЗАДАЧА-5). ТІЛЬКИ ЧИТАННЯ.

Запуск з кореня проєкту (де лежить .env):
    python scripts/probes/save_fixture.py                      # за вчора, обидва провайдери
    python scripts/probes/save_fixture.py --date 2026-11-05 --rows 4
    python scripts/probes/save_fixture.py --only upg
    python scripts/probes/save_fixture.py --only ukrnafta --date 2026-10-11 --days 30   # період: з дати N днів

Що робить:
- UPG: `POST /login`, потім одна сторінка `POST /fuel-card-history` за день;
- UkrNafta: `GET /common/transactions` за день (як `fuel/providers/ukrnafta.py`);
- з відповіді лишає по `--rows` рядків кожного виду (UkrNafta — за `typeId` і скасуванням),
  решту полів відповіді (meta, errorCode, ...) зберігає як є — вони потрібні для тестів пагінації;
- знеособлює: рядки й числа в полях, назва яких схожа на номер картки, власника, водія, авто,
  рахунок, контакт тощо, замінюються заглушками (однакове значення → однакова заглушка);
- пише `fuel/tests/fixtures/upg_transactions.json` і `ukrnafta_transactions.json`
  і друкує список замінених полів.

Токени й паролі не друкуються. ПЕРЕД КОМІТОМ переглянь файли фікстур очима (git diff):
поле з персональними даними під незвичною назвою скрипт може не впізнати.
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from fuel.providers.ukrnafta import UkrnaftaProvider  # noqa: E402
from fuel.providers.upg import UpgProvider  # noqa: E402

FIXTURES = ROOT / "fuel" / "tests" / "fixtures"
TIMEOUT = 60

# Поля з персональними або ідентифікуючими даними (за назвою ключа, без урахування регістру)
SENSITIVE = re.compile(
    r"card|holder|vin|driver|person|owner|client|customer|account|contract|plate|"
    r"phone|mail|login|user|token|name|surname|patronymic|address|inn|edrpou|tax|passport|"
    r"vehicle|auto|car_|number|remark|comment|note|description",
    re.IGNORECASE,
)
# Публічні назви, які потрібні тестам: пальне, АЗС, продукт
SAFE = re.compile(
    r"^(fuel|fuelname|service_station|station|terminalname|azsname|productname|errordescription|rownumber)$",
    re.IGNORECASE,
)
SAFE_PATHS = {("products", "name")}  # products[].name — назва пального UkrNafta


class Anonymizer:
    def __init__(self) -> None:
        self.map: dict[tuple[str, object], object] = {}
        self.counters: dict[str, int] = defaultdict(int)
        self.fields: set[str] = set()

    def fake(self, key: str, value):
        token = (key.lower(), value)
        if token not in self.map:
            self.counters[key.lower()] += 1
            n = self.counters[key.lower()]
            self.map[token] = 900000 + n if isinstance(value, int) else f"{key.upper()}-{n:03d}"
        return self.map[token]

    def walk(self, data, parent: str = ""):
        if isinstance(data, dict):
            return {k: self._value(parent, k, v) for k, v in data.items()}
        if isinstance(data, list):
            return [self.walk(item, parent) for item in data]
        return data

    def _value(self, parent: str, key: str, value):
        if isinstance(value, (dict, list)):
            return self.walk(value, key)
        if value in (None, "", 0) or isinstance(value, bool):
            return value
        sensitive = SENSITIVE.search(key) and not SAFE.match(key) and (parent, key) not in SAFE_PATHS
        if sensitive and isinstance(value, (str, int)):
            self.fields.add(f"{parent}.{key}" if parent else key)
            return self.fake(key, value)
        return value


def sample(rows: list[dict], kind, per_kind: int) -> list[dict]:
    """По per_kind рядків кожного виду, у вихідному порядку."""
    taken: dict[object, int] = defaultdict(int)
    result = []
    for row in rows:
        k = kind(row)
        if taken[k] < per_kind:
            taken[k] += 1
            result.append(row)
    return result


def fetch_upg(day: date, days: int) -> dict:
    provider = UpgProvider()
    resp = requests.post(
        provider.URL,
        data={"date_from": day.isoformat(), "date_to": (day + timedelta(days=days)).isoformat(), "page": 1},
        headers={"Accept": "application/json", "Authorization": f"Bearer {provider._login()}"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_ukrnafta(day: date, days: int) -> dict:
    resp = requests.get(
        UkrnaftaProvider.URL,
        params={
            "fromDate": day.isoformat(),
            "toDate": (day + timedelta(days=days)).isoformat(),
            "holderId": os.environ["UKRNAFTA_HOLDER_ID"],
            "page": 0,
            "limit": 1000,
            "operation_kind": 0,
            "with_cancelation": "true",
            "lang": "uk",
        },
        headers={"Accept": "application/json", "Authorization": f"Bearer {os.environ['UKRNAFTA_TOKEN']}"},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def save(name: str, payload: dict, list_key: str, kind, rows: int, day: date, days: int) -> None:
    all_rows = payload.get(list_key) or []
    payload = {**payload, list_key: sample(all_rows, kind, rows)}
    anon = Anonymizer()
    clean = anon.walk(payload)
    clean["_fixture"] = {
        "source": name,
        "date": day.isoformat(),
        "days": days,
        "rows_total": len(all_rows),
        "note": "знеособлено scripts/probes/save_fixture.py; рядків обрізано",
    }
    path = FIXTURES / f"{name}_transactions.json"
    path.write_text(json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    kinds = sorted({str(kind(r)) for r in all_rows})
    print(f"{name}: рядків у відповіді {len(all_rows)}, збережено {len(clean[list_key])}, види: {kinds}")
    print(f"  верхні поля відповіді: {sorted(k for k in payload if k != list_key)}")
    print(f"  знеособлено поля: {sorted(anon.fields)}")
    print(f"  файл: {path.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", type=date.fromisoformat, default=date.today() - timedelta(days=1))
    parser.add_argument("--days", type=int, default=1, help="тривалість періоду від --date, днів (типово 1)")
    parser.add_argument("--rows", type=int, default=3, help="рядків кожного виду (типово 3)")
    parser.add_argument("--only", choices=["upg", "ukrnafta"])
    args = parser.parse_args()

    if args.only in (None, "upg"):
        try:
            save(
                "upg",
                fetch_upg(args.date, args.days),
                "data",
                lambda r: "fuel",
                args.rows,
                args.date,
                args.days,
            )
        except Exception as exc:  # проба: показати й іти далі до іншого провайдера
            print(f"upg: помилка {type(exc).__name__}: {str(exc)[:300]}")
    if args.only in (None, "ukrnafta"):
        try:
            save(
                "ukrnafta",
                fetch_ukrnafta(args.date, args.days),
                "transactions",
                lambda r: (r.get("typeId"), bool(r.get("canceled"))),
                args.rows,
                args.date,
                args.days,
            )
        except Exception as exc:
            print(f"ukrnafta: помилка {type(exc).__name__}: {str(exc)[:300]}")


if __name__ == "__main__":
    main()
