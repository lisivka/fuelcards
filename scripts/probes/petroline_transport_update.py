"""Перевірка запису транспорту Petroline (PUT /api/Transports/update) на тимчасовому транспорті.

Запуск з кореня проєкту:
    python scripts/probes/petroline_transport_update.py            # лише читання
    python scripts/probes/petroline_transport_update.py --apply    # ЗАПИС: створити, змінити, видалити

Лише читання: як автомобільні картки (typeId 5) показують транспорт у Keys/list, довідник типів
транспорту, скільки транспортів мають кілька активних карток.
З --apply: створює транспорт «ТЕСТ» / «AA0000AA» без карток, змінює модель і номер, перечитує
і видаляє (навіть після помилки).
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

ALL = lambda s, t: {"orderBy": [{"field": "id", "order": 0}], "skip": s, "take": t}  # noqa: E731
FIELDS = ("id", "number", "model", "name", "typeName", "transportTypeId", "groupId", "status", "fuels")


def raw(client: PetrolineClient, method: str, path: str, body=None):
    if not client._authorized:
        client.login()
    resp = client.session.request(method, f"{BASE}{path}", json=body, timeout=TIMEOUT)
    print(f"{method} {path}: HTTP {resp.status_code}; відповідь: {resp.text[:400]!r}")
    return resp


def short(t: dict) -> dict:
    return {k: t.get(k) for k in FIELDS}


def find_test(client: PetrolineClient) -> list[dict]:
    return [t for t in client.post_paged("/api/Transports/list", ALL)
            if "ТЕСТ" in (t.get("model") or "") or "AA0000AA" in (t.get("number") or "")
            or "BB1111BB" in (t.get("number") or "")]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="РЕАЛЬНИЙ запис на тимчасовому транспорті")
    args = p.parse_args()

    client = PetrolineClient()
    keys = client.post_paged("/api/Keys/list", ALL)
    auto = [k for k in keys if k.get("typeId") == 5]
    print("Автомобільні картки, поля транспорту (перші 3):")
    for k in auto[:3]:
        print("  ", {f: k.get(f) for f in ("transportId", "transportName", "transportNumber", "personId",
                                           "personName", "status")})
    per_tr = Counter(k["transportId"] for k in keys if k.get("transportId") and k.get("status") == 3)
    print("Транспортів з кількома активними картками:", sum(1 for n in per_tr.values() if n > 1))
    print("Автомобільних карток без транспорту:", sum(1 for k in auto if not k.get("transportId")))
    print("Персональних карток із транспортом:", sum(1 for k in keys if k.get("typeId") == 1 and k.get("transportId")))
    raw(client, "GET", "/api/LibTransportType")
    transports = client.post_paged("/api/Transports/list", ALL)
    print(f"Транспортів: {len(transports)}; приклад:", json.dumps(short(transports[0]), ensure_ascii=False))
    if not args.apply:
        print("\nРежим лише читання. Для перевірки запису додайте --apply.")
        return

    if find_test(client):
        sys.exit("Вже є тестовий транспорт, спершу розберіться вручну")
    sample = transports[0]
    base = {"groupId": sample["groupId"], "fuels": [1], "typeId": sample.get("transportTypeId") or 2,
            "year": None, "vin": None, "color": None, "status": 3}
    raw(client, "POST", "/api/Transports/create", {**base, "model": "ТЕСТ", "number": "AA0000AA"})
    created = find_test(client)
    print("Створено:", json.dumps([short(t) for t in created], ensure_ascii=False))
    if len(created) != 1:
        sys.exit("Не вдалося однозначно знайти створений транспорт — перевірте кабінет")
    tid = created[0]["id"]
    try:
        raw(client, "PUT", "/api/Transports/update",
            {**base, "transportId": tid, "model": "ТЕСТ МОДЕЛЬ", "number": "BB1111BB"})
        now = [t for t in client.post_paged("/api/Transports/list", ALL) if t["id"] == tid]
        print("Після зміни:", json.dumps(short(now[0]), ensure_ascii=False) if now else "НЕ ЗНАЙДЕНО")
    finally:
        print()
        raw(client, "DELETE", f"/api/Transports/delete/{tid}")
        rest = [t for t in client.post_paged("/api/Transports/list", ALL) if t["id"] == tid]
        print("Після видалення:", "транспорту немає" if not rest else f"ЛИШИВСЯ: {short(rest[0])}")


if __name__ == "__main__":
    main()
