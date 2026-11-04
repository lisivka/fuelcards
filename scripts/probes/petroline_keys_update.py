"""Перевірка запису картки Petroline через PUT /api/Keys/update (ЗАДАЧА-12).

Запуск з кореня проєкту:
    python scripts/probes/petroline_keys_update.py --number 6100212E1C            # лише читання
    python scripts/probes/petroline_keys_update.py --number 6100212E1C --apply    # ЗАПИС: блок -> розблок

Без --apply скрипт лише читає: знаходить картку, показує тіло, яке було б надіслано
(PIN замасковано), і рахує, у скількох картках PIN не порожній.
З --apply: ставить статус 5 (заблокована), перечитує, повертає початковий статус, перечитує
і показує, які поля змінились. Початковий статус повертається навіть після помилки.
Токен, пароль і PIN у вивід не потрапляють.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from fuel.providers.petroline_api import BASE, TIMEOUT, PetrolineClient  # noqa: E402

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
STATUS_BLOCKED = 5


def masked(rec: dict) -> dict:
    return {k: ("***" if k == "pin" and v not in (None, "") else v) for k, v in rec.items()}


def show(label: str, data) -> None:
    print(f"\n=== {label}")
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def find_key(client: PetrolineClient, number: str) -> dict:
    body = {
        "where": {
            "operatorType": 1,
            "operands": [
                {"operatorType": 0, "operands": None, "field": "number", "filterType": 1, "value": number}
            ],
            "field": None,
            "filterType": 0,
            "value": None,
        },
        "orderBy": [{"field": "id", "order": 0}],
        "skip": 0,
        "take": 5,
    }
    rows = client.post("/api/Keys/list", body)
    rows = rows if isinstance(rows, list) else []
    print(f"Keys/list з фільтром number={number}: записів {len(rows)}")
    found = [r for r in rows if str(r.get("number")) == number]
    if len(rows) == 1 and found:
        print("Фільтр за number працює (повернуто рівно одну потрібну картку).")
        return found[0]
    print("Фільтр не звузив вибірку — шукаю в повному списку.")
    all_rows = client.post_paged(
        "/api/Keys/list", lambda s, t: {"orderBy": [{"field": "id", "order": 0}], "skip": s, "take": t}
    )
    found = [r for r in all_rows if str(r.get("number")) == number]
    if not found:
        sys.exit(f"Картку {number} не знайдено")
    return found[0]


def pin_stats(client: PetrolineClient) -> None:
    rows = client.post_paged(
        "/api/Keys/list", lambda s, t: {"orderBy": [{"field": "id", "order": 0}], "skip": s, "take": t}
    )
    with_pin = sum(1 for r in rows if r.get("pin") not in (None, ""))
    print(f"\nКарток усього: {len(rows)}; з непорожнім pin у Keys/list: {with_pin}")


def key_dto(rec: dict, **changes) -> dict:
    body = {f: rec.get(f) for f in KEY_DTO_FIELDS}
    body.update(changes)
    return body


def put_update(client: PetrolineClient, body: dict) -> None:
    resp = client.session.put(f"{BASE}/api/Keys/update", json=body, timeout=TIMEOUT)
    print(f"PUT Keys/update status={body['status']}: HTTP {resp.status_code}; відповідь: {resp.text[:300]!r}")
    resp.raise_for_status()


def diff(before: dict, after: dict) -> dict:
    return {
        k: [before.get(k), after.get(k)]
        for k in sorted(set(before) | set(after))
        if before.get(k) != after.get(k) and k != "pin"
    } | ({"pin": "ЗМІНИВСЯ"} if before.get("pin") != after.get("pin") else {})


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="Перевірка PUT /api/Keys/update")
    p.add_argument("--number", required=True, help="Ідентифікатор картки (Keys.number)")
    p.add_argument("--apply", action="store_true", help="РЕАЛЬНИЙ запис: заблокувати і розблокувати")
    args = p.parse_args()

    client = PetrolineClient()
    original = find_key(client, args.number)
    show("Картка (PIN замасковано)", masked(original))
    pin_stats(client)

    body = key_dto(original, status=STATUS_BLOCKED)
    show("Тіло, яке буде надіслано для блокування", masked(body))
    if not args.apply:
        print("\nРежим лише читання: нічого не надіслано. Для запису додайте --apply.")
        return

    start_status = original.get("status")
    try:
        put_update(client, body)
        blocked = find_key(client, args.number)
        show("Зміни після блокування", diff(original, blocked))
    finally:
        put_update(client, key_dto(original, status=start_status))
        restored = find_key(client, args.number)
        show("Відмінності від початкового стану після розблокування", diff(original, restored))


if __name__ == "__main__":
    main()
