"""Розвідка нового API Petroline (https://apiv2.petroline.in.ua). ТІЛЬКИ ЧИТАННЯ.

Запуск з кореня проєкту (де лежить .env):
    python test_petroline_v2.py
    python test_petroline_v2.py --from 2026-10-28 --to 2026-10-29
    python test_petroline_v2.py --no-db          # без порівняння з локальною базою

Скрипт нічого не змінює в Petroline: отримує токен і читає списки.
Токен і пароль у вивід не потрапляють, PIN замінюється на ***.
Результат також зберігається у petroline_v2_probe.json: там реальні дані, не коміть його в git.
"""
import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

BASE = "https://apiv2.petroline.in.ua"
TIMEOUT = 60
KYIV = ZoneInfo("Europe/Kyiv")
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
SENSITIVE_KEYS = {"pin", "password", "token", "secret"}


# ---------- допоміжне ----------

def redact(obj):
    """Ховає чутливі поля (PIN тощо) перед друком і збереженням."""
    if isinstance(obj, dict):
        return {
            k: ("***" if k.lower() in SENSITIVE_KEYS and v not in (None, "") else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(x) for x in obj]
    return obj


def show(data, limit=1500):
    data = redact(data)
    text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False, indent=2, default=str)
    print(text if len(text) <= limit else text[:limit] + "\n... (обрізано)")


def as_list(data):
    """Відповідь може бути масивом або об'єктом-обгорткою зі списком усередині."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "items", "result", "results", "list", "records"):
            if isinstance(data.get(key), list):
                return data[key]
        for value in data.values():
            if isinstance(value, list):
                return value
    return []


def iso_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def day_bounds(d_from, d_to):
    """Межі періоду за київським часом: від 00:00 d_from до 23:59:59 d_to."""
    start = datetime.combine(d_from, time.min, KYIV)
    end = datetime.combine(d_to + timedelta(days=1), time.min, KYIV) - timedelta(seconds=1)
    return start, end


def call(session, method, path, body=None, label="", verbose=True):
    try:
        resp = session.request(method, BASE + path, json=body, timeout=TIMEOUT)
    except requests.RequestException as exc:
        print(f"\n=== {label or path}: ПОМИЛКА З'ЄДНАННЯ: {exc}")
        return None, None
    try:
        data = resp.json()
    except ValueError:
        data = resp.text
    if verbose:
        print(f"\n=== {label or path}: HTTP {resp.status_code} ({resp.elapsed.total_seconds():.1f} с)")
    return resp.status_code, data


def get_token(login, password):
    try:
        resp = requests.post(
            f"{BASE}/api/Auth/token", json={"login": login, "password": password}, timeout=TIMEOUT
        )
    except requests.RequestException as exc:
        sys.exit(f"Не вдалося з'єднатися з {BASE}: {exc}")
    print(f"=== Auth/token: HTTP {resp.status_code}")
    if resp.status_code != 200:
        sys.exit(f"Токен не отримано. Відповідь: {resp.text[:500]}")
    try:
        token = (resp.json() or {}).get("token")
    except ValueError:
        token = None
    if not token:
        sys.exit("У відповіді немає поля token")
    return token


# ---------- варіанти тіл запитів ----------

def list_variants():
    by_id = [{"field": "id", "order": 0}]
    return {
        "A: мінімальний (сортування за id)":
            lambda skip, take: {"orderBy": by_id, "skip": skip, "take": take},
        "B: where=null":
            lambda skip, take: {"where": None, "orderBy": by_id, "skip": skip, "take": take},
        "C: сортування за name":
            lambda skip, take: {"orderBy": [{"field": "name", "order": 0}], "skip": skip, "take": take},
    }


def tx_variants(d_from, d_to):
    start, end = day_bounds(d_from, d_to)
    s, e = iso_utc(start), iso_utc(end)
    date_obj = {"start": s, "end": e}
    order = [{"field": "id", "order": 0}]

    def where_dates():
        leaf = lambda filter_type, value: {  # noqa: E731
            "operatorType": 0, "operands": None, "field": "date", "filterType": filter_type, "value": value,
        }
        group = {"operatorType": 1, "operands": [leaf(6, s), leaf(3, e)],
                 "field": None, "filterType": 0, "value": None}
        return {"operatorType": 1, "operands": [group], "field": None, "filterType": 0, "value": None}

    return {
        "A: мінімальний (filter.date)":
            lambda skip, take: {"filter": {"date": date_obj},
                                "loadOption": {"orderBy": order, "skip": skip, "take": take}},
        "B: filter.date + порожні filters/search":
            lambda skip, take: {"filter": {"filters": [], "date": date_obj, "search": ""},
                                "loadOption": {"orderBy": order, "skip": skip, "take": take}},
        "C: дата лише через loadOption.where":
            lambda skip, take: {"loadOption": {"where": where_dates(), "orderBy": order,
                                               "skip": skip, "take": take}},
    }


def fetch_paged(session, path, variants, label, take, limit):
    """Пробує варіанти тіла, першим, що дав HTTP 200, вичитує сторінки. Повертає (factory, records)."""
    make, first = None, None
    for name, factory in variants.items():
        status, data = call(session, "POST", path, factory(0, take), f"{label} [{name}]")
        if status == 200:
            make, first = factory, data
            print(f"Спрацював варіант тіла: {name}")
            break
        show(data, 600)
    if make is None:
        print(f"{label}: жоден варіант тіла не спрацював")
        return None, []
    if isinstance(first, dict):
        print("Відповідь це об'єкт із ключами:", list(first))
    page = as_list(first)
    records = list(page)
    while len(page) == take and len(records) < limit:
        status, data = call(session, "POST", path, make(len(records), take),
                            f"{label} skip={len(records)}", verbose=False)
        if status != 200:
            print(f"  сторінка skip={len(records)}: HTTP {status}")
            break
        page = as_list(data)
        records.extend(page)
        print(f"  отримано ще {len(page)} (разом {len(records)})")
    return make, records


# ---------- порівняння з локальною базою ----------

def setup_django():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
        import django

        django.setup()
        from fuel.models import Card, Provider, Transaction
        from fuel.providers.base import aware, parse_iso

        return Card, Provider, Transaction, aware, parse_iso
    except Exception as exc:  # noqa: BLE001
        print(f"\n[БД] порівняння пропущено: {exc}")
        return None


def v2_times(rec, aware, parse_iso):
    """Можливі трактування дати з v2 (залежно від того, чи є часовий пояс у рядку)."""
    keys = sorted(
        (k for k, v in rec.items() if "date" in k.lower() and isinstance(v, str)),
        key=lambda k: k.lower() != "date",
    )
    for key in keys:
        try:
            dt = parse_iso(rec[key])
        except ValueError:
            continue
        if dt.tzinfo is None:
            return [("київський час (без поясу)", aware(dt)), ("UTC (без поясу)", dt.replace(tzinfo=timezone.utc))]
        return [("з поясом із відповіді", dt)]
    return []


def numeric_fields(rec):
    pattern = re.compile("value|volume|liter|amount|sum|price|cost", re.I)
    return {k: v for k, v in rec.items() if isinstance(v, (int, float)) and pattern.search(k)}


def compare_transactions(env, records, d_from, d_to):
    Card, Provider, Transaction, aware, parse_iso = env
    db = list(Transaction.objects.filter(
        provider=Provider.PETROLINE.value, timestamp__date__gte=d_from, timestamp__date__lte=d_to,
    ))
    db_by_id = {t.external_id: t for t in db}
    print("\n=== Порівняння з локальною базою (Petroline, поточне API) ===")
    print(f"v2: {len(records)} записів; база: {len(db)} за {d_from}..{d_to}")
    print("Приклад external_id у базі:", [t.external_id for t in db[:5]])
    print("Приклад id у v2:", [rec.get("id") for rec in records[:5]])

    matched, unmatched = 0, []
    for rec in records:
        ids = {str(rec[k]) for k in ("id", "idString") if rec.get(k) not in (None, "")}
        if ids & db_by_id.keys():
            matched += 1
        else:
            unmatched.append(rec)
    print(f"Збіг за ID: {matched} із {len(records)} записів v2")

    pairs = []
    for rec in unmatched[:200]:
        best = None
        for label, ts in v2_times(rec, aware, parse_iso):
            for t in db:
                diff = abs((t.timestamp - ts).total_seconds())
                if best is None or diff < best[0]:
                    best = (diff, label, t)
        if best and best[0] <= 120:
            pairs.append((rec, best))
    if pairs:
        print(f"\nБез збігу за ID, але зіставляються за часом (до 2 хв): {len(pairs)}")
        for rec, (diff, label, t) in pairs[:12]:
            print(f"  v2 id={rec.get('id')} <-> база external_id={t.external_id}; різниця {diff:.0f} с; "
                  f"час v2 як: {label}; поля v2: {numeric_fields(rec)}; база liters={t.liters}")

    if not db:
        print("\nВисновок: у базі немає транзакцій Petroline за цей період. Змініть --from/--to.")
    elif matched >= 0.9 * len(db):
        print("\nВисновок: ID збігаються, перехід на v2 не створить дублікатів.")
    elif pairs:
        print("\nВисновок: ID НЕ збігаються (записи зіставляються лише за часом). "
              "Для переходу потрібна стратегія проти дублікатів.")
    else:
        print("\nВисновок: зіставити записи не вдалося (інший формат дат чи порожня відповідь).")


def compare_cards(env, records):
    Card, Provider, *_ = env
    cards = list(Card.objects.filter(provider=Provider.PETROLINE.value))
    norm = lambda s: re.sub(r"\s+", "", str(s)).upper()  # noqa: E731
    db_exact = {c.number: c for c in cards}
    db_norm = {norm(c.number): c for c in cards}
    exact, normalized, matched = Counter(), Counter(), set()
    for rec in records:
        for key, val in rec.items():
            if isinstance(val, bool) or not isinstance(val, (str, int)) or val in ("", None):
                continue
            if str(val) in db_exact:
                exact[key] += 1
                matched.add(db_exact[str(val)].pk)
            elif norm(val) in db_norm:
                normalized[key] += 1
                matched.add(db_norm[norm(val)].pk)
    print("\n=== Порівняння карток із локальною базою (Petroline) ===")
    print(f"API: {len(records)} карток; база: {len(cards)} карток Petroline; знайдено збіг: {len(matched)}")
    print("Поля API, що збіглись із номером у базі точно:", dict(exact) or "немає")
    print("Поля, що збіглись лише після прибирання пробілів/регістру:", dict(normalized) or "немає")
    missing = [c.number for c in cards if c.pk not in matched]
    print(f"Картки бази без пари в API: {len(missing)}; приклади: {missing[:10]}")
    print("Приклад номерів у базі:", [c.number for c in cards[:5]])


# ---------- головний сценарій ----------

def parse_args():
    today = datetime.now(KYIV).date()
    p = argparse.ArgumentParser(description="Розвідка нового API Petroline (тільки читання)")
    p.add_argument("--from", dest="date_from", type=date.fromisoformat, default=today - timedelta(days=1))
    p.add_argument("--to", dest="date_to", type=date.fromisoformat, default=today)
    p.add_argument("--take", type=int, default=100, help="розмір сторінки")
    p.add_argument("--max", dest="limit", type=int, default=1000, help="максимум записів на запит")
    p.add_argument("--login-env", default="PETROLINE_LOGIN", help="назва змінної .env з логіном")
    p.add_argument("--password-env", default="PETROLINE_PASSWORD", help="назва змінної .env з паролем")
    p.add_argument("--no-db", action="store_true", help="не порівнювати з локальною базою")
    p.add_argument("--out", default="petroline_v2_probe.json")
    return p.parse_args()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args()
    # load_dotenv(Path(__file__).resolve().parent / ".env")
    BASE_DIR = Path(__file__).resolve().parent
    load_dotenv(BASE_DIR / '.env')
    login = (os.environ.get("PETROLINE_LOGIN") or os.environ.get(args.env, "")).strip()
    password = (os.environ.get("PETROLINE_PASS") or os.environ.get(args.env, "")).strip()
    # login = os.environ.get("PETROLINE_LOGIN", "").strip()
    # password = os.environ.get("PETROLINE_PASS", "").strip()
    print(f"Використовуються змінні .env: {args.login_env}={login[:2]}***, {args.password_env}={'***' if password else '(порожньо)'}")
    if not login or not password:
        sys.exit(f"Не знайдено логін/пароль у .env (змінні {args.login_env} / {args.password_env}). "
                 "Вкажіть назви через --login-env / --password-env.")
    if SHA256_RE.match(password):
        sys.exit("У .env схоже на SHA256-хеш пароля. Нове API очікує звичайний пароль: "
                 "додайте PETROLINE_V2_PASSWORD у .env.")
    print(f"Логін: {login[:2]}***, період {args.date_from}..{args.date_to} (київський час)")

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {get_token(login, password)}", "Accept": "application/json"})
    results = {"period": [str(args.date_from), str(args.date_to)]}

    print("\n########## 1. Довідники ##########")
    status, data = call(session, "GET", "/api/Keys/getKeyTypes", label="Keys/getKeyTypes")
    show(data, 1500)
    results["key_types"] = data
    status, data = call(session, "GET", "/api/Groups/list", label="Groups/list (перші 3)")
    groups = as_list(data)
    show(groups[:3], 1500)
    print(f"Усього груп: {len(groups)}")
    results["groups_sample"] = groups[:3]
    for path, label in (("/api/Persons/list", "Persons"), ("/api/Transports/list", "Transports")):
        _, sample = fetch_paged(session, path, list_variants(), f"{label}/list", 3, 3)
        show(sample[:2], 2500)
        results[f"{label.lower()}_sample"] = sample[:2]

    print("\n########## 2. Картки (Keys/list) ##########")
    _, keys = fetch_paged(session, "/api/Keys/list", list_variants(), "Keys/list", args.take, args.limit)
    print(f"Усього карток отримано: {len(keys)}")
    if keys:
        print("Поля запису:", sorted(keys[0]))
        for rec in keys[:2]:
            show(rec, 2500)
    results["keys_total"] = len(keys)
    results["keys_sample"] = keys[:5]

    print("\n########## 3. Транзакції v2 ##########")
    make, tx = fetch_paged(session, "/api/TrkTransactions/v2/list",
                           tx_variants(args.date_from, args.date_to),
                           "TrkTransactions/v2/list", args.take, args.limit)
    print(f"Усього транзакцій отримано: {len(tx)}")
    if tx:
        print("Поля запису:", sorted(tx[0]))
        for rec in tx[:2]:
            show(rec, 3000)
    if make:
        _, total = call(session, "POST", "/api/TrkTransactions/v2/list/total",
                        make(0, args.take), "TrkTransactions/v2/list/total")
        show(total, 600)
        results["total_response"] = total
    results["transactions_total"] = len(tx)
    results["transactions_sample"] = tx[:5]

    if not args.no_db:
        env = setup_django()
        if env:
            if keys:
                compare_cards(env, keys)
            if tx:
                compare_transactions(env, tx, args.date_from, args.date_to)

    Path(args.out).write_text(
        json.dumps(redact(results), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(f"\nЗразки збережено в {args.out} (не коміть цей файл).")


if __name__ == "__main__":
    main()