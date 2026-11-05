"""Перевірка: картка без прив'язки -> «Персональна» з новим водієм, потім повернення як було.

Запуск з кореня проєкту:
    python scripts/probes/petroline_card_relink.py --number 06003C4C03            # лише читання
    python scripts/probes/petroline_card_relink.py --number 06003C4C03 --apply    # ЗАПИС

З --apply: створює водія «ТЕСТ API» (пальне ДП), змінює картці typeId на 1 і personId на нового водія,
перечитує, повертає початкові typeId/personId/transportId і видаляє водія (навіть після помилки).
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
from fuel.providers.petroline_cards import KEY_DTO_FIELDS, PetrolineCards  # noqa: E402

ALL = lambda s, t: {"orderBy": [{"field": "id", "order": 0}], "skip": s, "take": t}  # noqa: E731
SHOW = ("id", "number", "typeId", "typeName", "personId", "personName", "transportId", "status", "groupId")


def raw(client: PetrolineClient, method: str, path: str, body=None):
    if not client._authorized:
        client.login()
    resp = client.session.request(method, f"{BASE}{path}", json=body, timeout=TIMEOUT)
    print(f"{method} {path}: HTTP {resp.status_code}; відповідь: {resp.text[:400]!r}")
    return resp


def short(rec: dict) -> str:
    return json.dumps({k: rec.get(k) for k in SHOW}, ensure_ascii=False)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--number", required=True)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()

    client = PetrolineClient()
    cards = PetrolineCards(client=client)
    original = cards.find_card(args.number)
    print("Картка:", short(original))
    if original.get("personId") or original.get("transportId"):
        sys.exit("Картка вже прив'язана — для перевірки потрібна картка без водія й транспорту")
    if not args.apply:
        print("Режим лише читання. Для перевірки додайте --apply.")
        return

    resp = raw(client, "POST", "/api/Persons/create", {
        "name": "", "surname": "ТЕСТ API", "middlename": "", "groupId": original["groupId"],
        "email": None, "phone": None, "fuels": [1], "status": 3,
    })
    person_id = int(str(resp.json()).strip('"'))
    print("Створено водія id =", person_id)
    restore = {f: original.get(f) for f in KEY_DTO_FIELDS}
    try:
        raw(client, "PUT", "/api/Keys/update", {**restore, "typeId": 1, "personId": person_id})
        print("Після прив'язки:", short(cards.find_card(args.number)))
    finally:
        raw(client, "PUT", "/api/Keys/update", restore)
        back = cards.find_card(args.number)
        print("Після повернення:", short(back))
        diff = {f: [original.get(f), back.get(f)] for f in KEY_DTO_FIELDS if original.get(f) != back.get(f)}
        print("Відмінності від початкового стану:", diff or "немає")
        raw(client, "DELETE", f"/api/Persons/delete/{person_id}")
        rest = [x for x in client.post_paged("/api/Persons/list", ALL) if x["id"] == person_id]
        print("Водій після видалення:", "немає" if not rest else f"ЛИШИВСЯ {rest[0]['name']!r}")


if __name__ == "__main__":
    main()
