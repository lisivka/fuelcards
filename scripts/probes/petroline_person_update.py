"""Перевірка запису поля «Водій» у Petroline (PUT /api/Persons/update) на тимчасовому водієві.

Запуск з кореня проєкту:
    python scripts/probes/petroline_person_update.py            # лише читання: статистика
    python scripts/probes/petroline_person_update.py --apply    # ЗАПИС: створити тест. водія, змінити, видалити

Лише читання: скільки водіїв мають «|» у імені і скільки водіїв мають кілька карток.
З --apply: створює водія «ТЕСТ API» без карток, записує «AA0000AA | Тест Тестович»,
перечитує, як Petroline показує ім'я, і видаляє водія (навіть після помилки).
"""

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402

django.setup()

from fuel.providers.petroline_api import BASE, TIMEOUT, PetrolineClient  # noqa: E402

TEST_SURNAME, TEST_NAME = "ТЕСТ", "API"
NEW_VALUE = "AA0000AA | Тест Тестович"
ALL = lambda s, t: {"orderBy": [{"field": "id", "order": 0}], "skip": s, "take": t}  # noqa: E731


def raw(client: PetrolineClient, method: str, path: str, body=None):
    if not client._authorized:
        client.login()
    resp = client.session.request(method, f"{BASE}{path}", json=body, timeout=TIMEOUT)
    print(f"{method} {path}: HTTP {resp.status_code}; відповідь: {resp.text[:400]!r}")
    return resp


def find_test_persons(client: PetrolineClient) -> list[dict]:
    return [p for p in client.post_paged("/api/Persons/list", ALL) if "ТЕСТ" in (p.get("name") or "")
            or "AA0000AA" in (p.get("name") or "")]


def stats(client: PetrolineClient) -> list[dict]:
    persons = client.post_paged("/api/Persons/list", ALL)
    keys = client.post_paged("/api/Keys/list", ALL)
    with_bar = [p["name"] for p in persons if "|" in (p.get("name") or "")]
    per_person = Counter(k.get("personId") for k in keys if k.get("personId"))
    multi = {pid: n for pid, n in per_person.items() if n > 1}
    personal_multi = {pid: n for pid, n in multi.items()
                      if any(k.get("personId") == pid and k.get("typeId") == 1 for k in keys)}
    print(f"Водіїв: {len(persons)}; з «|» у імені: {len(with_bar)}; приклади: {with_bar[:5]}")
    print(f"Карток: {len(keys)}; водіїв із кількома картками: {len(multi)} "
          f"(з них мають персональну: {len(personal_multi)}); приклад: {list(personal_multi.items())[:5]}")
    print("Типи карток:", Counter((k.get("typeId"), k.get("typeName")) for k in keys))
    return persons


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="РЕАЛЬНИЙ запис на тимчасовому водієві")
    args = p.parse_args()

    client = PetrolineClient()
    persons = stats(client)
    if not args.apply:
        print("\nРежим лише читання. Для перевірки запису додайте --apply.")
        return

    leftovers = find_test_persons(client)
    if leftovers:
        sys.exit(f"Вже є тестові водії, спершу розберіться вручну: {[(x['id'], x['name']) for x in leftovers]}")
    sample = next(x for x in persons if x.get("groupId"))
    group_id, fuels = sample["groupId"], [1]
    create = {"name": TEST_NAME, "surname": TEST_SURNAME, "middlename": None, "groupId": group_id,
              "email": None, "phone": None, "fuels": fuels, "status": 3}
    raw(client, "POST", "/api/Persons/create", create)
    created = find_test_persons(client)
    print("Створено:", json.dumps([(x["id"], x["name"], x.get("status")) for x in created], ensure_ascii=False))
    if len(created) != 1:
        sys.exit("Не вдалося однозначно знайти створеного водія — зупиняюсь, перевірте кабінет")
    person_id = created[0]["id"]
    try:
        for label, body in (
            ("усе в surname, name порожнє", {"name": "", "surname": NEW_VALUE, "middlename": ""}),
            ("усе в name, surname = «.»", {"name": NEW_VALUE, "surname": ".", "middlename": ""}),
        ):
            update = {"id": person_id, "groupId": group_id, "fuels": fuels, "status": 3,
                      "email": None, "phone": None, **body}
            print(f"\n--- Варіант: {label}")
            raw(client, "PUT", "/api/Persons/update", update)
            now = [x for x in client.post_paged("/api/Persons/list", ALL) if x["id"] == person_id]
            print("Petroline показує name:", repr(now[0]["name"]) if now else "НЕ ЗНАЙДЕНО")
    finally:
        print()
        raw(client, "DELETE", f"/api/Persons/delete/{person_id}")
        rest = [x for x in client.post_paged("/api/Persons/list", ALL) if x["id"] == person_id]
        print("Після видалення:", "водія немає" if not rest else f"ЛИШИВСЯ: {rest[0]['name']!r}, status={rest[0].get('status')}")


if __name__ == "__main__":
    main()
