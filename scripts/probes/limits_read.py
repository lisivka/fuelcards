"""Розвідка лімітів карток UkrNafta і Petroline (ЗАДАЧА-4, етап 0). ТІЛЬКИ ЧИТАННЯ.

Запуск з кореня проєкту (де лежить .env):
    python scripts/probes/limits_read.py --out <шлях.json>
    python scripts/probes/limits_read.py --cards 5 --out <шлях.json>   # більше карток UkrNafta

Що робить (лише GET і POST-читання списків, нічого не змінює):
- UkrNafta: довідник типів лімітів `GET /common/limits/types`, ліміти перших N активних карток
  `GET /cards/{cardId}/limits` (а також кілька заблокованих — щоб побачити різницю);
- Petroline: `PersonLimit/list` повністю, перша сторінка `TransportLimit/list`, `LibFuelType`,
  статистика значень полів і зв'язок лімітів з типами карток водія;
- swagger обох API (якщо віддається) — перелік ендпоінтів зі словом «limit».

Токени й паролі не друкуються. Файл --out містить реальні дані — не комітити, знеособлені
фікстури робляться з нього окремо.
"""

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from fuel.providers.petroline_api import BASE as PL_BASE  # noqa: E402
from fuel.providers.petroline_api import TIMEOUT, PetrolineClient  # noqa: E402

UN_BASE = "https://naftacards.ukrnafta.com/public-api"
ALL = lambda s, t: {"orderBy": [{"field": "id", "order": 0}], "skip": s, "take": t}  # noqa: E731


def show(title: str, data, limit: int = 2500) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    print(f"\n=== {title}")
    print(text if len(text) <= limit else text[:limit] + "\n... (обрізано)")


def un_get(path: str, params: dict | None = None):
    token = os.environ.get("UKRNAFTA_TOKEN", "").strip()
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    resp = requests.get(
        f"{UN_BASE}{path}", params={"lang": "uk", **(params or {})}, headers=headers, timeout=60
    )
    try:
        data = resp.json()
    except ValueError:
        data = resp.text[:500]
    print(f"GET {path}: HTTP {resp.status_code}")
    return resp.status_code, data


def swagger_limits(url: str) -> dict:
    """Ендпоінти зі словом limit у swagger (якщо він доступний без авторизації)."""
    try:
        resp = requests.get(url, timeout=30)
        spec = resp.json()
    except (requests.RequestException, ValueError):
        print(f"swagger {url}: недоступний")
        return {}
    paths = spec.get("paths") or {}
    found = {p: sorted(ops) for p, ops in paths.items() if "limit" in p.lower()}
    print(f"swagger {url}: усього шляхів {len(paths)}, з limit: {len(found)}")
    schemas = (spec.get("components") or {}).get("schemas") or {}
    limit_schemas = {name: s for name, s in schemas.items() if "limit" in name.lower()}
    return {"paths": found, "schemas": limit_schemas}


def probe_ukrnafta(n_cards: int) -> dict:
    out: dict = {}
    print("\n########## UkrNafta ##########")
    out["types"] = un_get("/common/limits/types")[1]
    show("Типи лімітів", out["types"])
    account = os.environ.get("UKRNAFTA_ACCOUNT_ID", "").strip()
    _, data = un_get(f"/accounts/{account}/cards")
    cards = (data or {}).get("cards") or [] if isinstance(data, dict) else []
    print(f"Карток: {len(cards)}; статуси: {dict(Counter(c.get('status') for c in cards))}")
    chosen = [c for c in cards if c.get("status") == 0][:n_cards] + [
        c for c in cards if c.get("status") == 1
    ][:2]
    out["cards"] = {}
    for card in chosen:
        card_id = card.get("cardId")
        status, limits = un_get(f"/cards/{card_id}/limits")
        out["cards"][str(card_id)] = {"card_status": card.get("status"), "http": status, "limits": limits}
        show(f"Ліміти картки …{str(card_id)[-4:]} (статус {card.get('status')})", limits, 3000)
    out["swagger"] = {}
    for url in (f"{UN_BASE}/swagger.json", f"{UN_BASE}/v3/api-docs", f"{UN_BASE}/swagger/v1/swagger.json"):
        out["swagger"][url] = swagger_limits(url)
    return out


def probe_petroline() -> dict:
    out: dict = {}
    print("\n########## Petroline ##########")
    client = PetrolineClient()
    limits = client.post_paged("/api/PersonLimit/list", ALL)
    out["person_limits"] = limits
    print(f"PersonLimit: {len(limits)}")
    if limits:
        print("Поля:", sorted(limits[0]))
        show("Перші 3 PersonLimit", limits[:3])
        for field in sorted(limits[0]):
            values = Counter(json.dumps(r.get(field), ensure_ascii=False, default=str) for r in limits)
            if len(values) <= 15:
                print(f"  {field}: {dict(values)}")
            else:
                print(f"  {field}: {len(values)} різних значень")
    out["transport_limits_page"] = client.post("/api/TransportLimit/list", ALL(0, 5))
    show("TransportLimit (перша сторінка)", out["transport_limits_page"], 1500)
    out["fuel_types"] = client._request("get", "/api/LibFuelType", None)
    show("LibFuelType", out["fuel_types"], 1500)

    keys = client.post_paged("/api/Keys/list", ALL)
    types_by_person: dict = defaultdict(Counter)
    for key in keys:
        if key.get("personId"):
            types_by_person[key["personId"]][key.get("typeId")] += 1
    with_limit = Counter()
    for rec in limits:
        for type_id in types_by_person.get(rec.get("personId"), Counter({"без картки": 1})):
            with_limit[type_id] += 1
    print(f"Водіїв з лімітами за типом їхніх карток: {dict(with_limit)}")
    per_person = Counter(r.get("personId") for r in limits)
    print(f"Лімітів на одного водія: {dict(Counter(per_person.values()))}")
    out["limit_card_types"] = {str(k): v for k, v in with_limit.items()}

    out["swagger"] = {}
    for url in (f"{PL_BASE}/swagger/v1/swagger.json", f"{PL_BASE}/swagger/swagger.json"):
        found = swagger_limits(url)
        out["swagger"][url] = found
        if found.get("paths"):
            show("Ендпоінти лімітів Petroline", found["paths"])
            show("Схеми лімітів Petroline", found["schemas"], 6000)
            break
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cards", type=int, default=3, help="скільки активних карток UkrNafta читати")
    parser.add_argument("--out", required=True, help="куди зберегти сирі відповіді (НЕ в git)")
    parser.add_argument("--only", choices=("ukrnafta", "petroline"), help="лише один провайдер")
    args = parser.parse_args()

    result: dict = {}
    for name, fn in (("ukrnafta", lambda: probe_ukrnafta(args.cards)), ("petroline", probe_petroline)):
        if args.only and args.only != name:
            continue
        try:
            result[name] = fn()
        except Exception as exc:  # noqa: BLE001 — збій одного провайдера не зупиняє іншого
            print(f"\n!!! {name}: {type(exc).__name__}: {exc}")
            result[name] = {"error": str(exc)}
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nЗбережено в {args.out} (реальні дані — не комітити).")


if __name__ == "__main__":
    print(f"Таймаут запитів: {TIMEOUT} с")
    main()
