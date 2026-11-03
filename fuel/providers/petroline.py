"""Транзакції Petroline.

Основний шлях — новий API (`POST /api/TrkTransactions/v2/list`). Старий API
(`fms.petroline.in.ua:8055/api/TransactionsGet`) лишається запасним: `PETROLINE_API=legacy` у .env.
Перевірено на живих даних (2026-11): `id` нового API = `TransactionID` старого, тож перемикання
не створює дублікатів.
"""

import hashlib
import os
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import requests
from django.utils import timezone as dj_timezone

from .base import BaseProvider, NormalizedTx, aware, parse_iso, to_dec
from .petroline_api import PetrolineClient

TX_PATH = "/api/TrkTransactions/v2/list"


def _divisor() -> Decimal:
    """`value` в обох API — мілілітри."""
    return Decimal(os.environ.get("PETROLINE_VALUE_DIVISOR") or "1000")


def _clean(text) -> str:
    return " ".join(str(text or "").split())


def period_bounds_utc(date_from: date, date_to: date) -> tuple[str, str]:
    """Від 00:00 date_from до 23:59:59 date_to за київським часом, у форматі UTC для API."""
    tz = dj_timezone.get_default_timezone()
    start = datetime.combine(date_from, time.min, tz)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tz) - timedelta(seconds=1)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"
    return start.astimezone(timezone.utc).strftime(fmt), end.astimezone(timezone.utc).strftime(fmt)


class PetrolineProvider(BaseProvider):
    name = "petroline"

    def __init__(self, client: PetrolineClient | None = None):
        self.client = client

    def fetch(self, date_from: date, date_to: date) -> list[NormalizedTx]:
        if (os.environ.get("PETROLINE_API") or "v2").strip().lower() == "legacy":
            return PetrolineLegacyProvider().fetch(date_from, date_to)
        start, end = period_bounds_utc(date_from, date_to)

        def make_body(skip: int, take: int) -> dict:
            return {
                "filter": {"date": {"start": start, "end": end}},
                "loadOption": {"orderBy": [{"field": "id", "order": 0}], "skip": skip, "take": take},
            }

        client = self.client or PetrolineClient()
        rows = client.post_paged(TX_PATH, make_body)
        divisor = _divisor()
        return [self._normalize(row, divisor) for row in rows if row.get("id") is not None]

    def _normalize(self, row: dict, divisor: Decimal) -> NormalizedTx:
        value = to_dec(row.get("value"))
        return NormalizedTx(
            provider=self.name,
            external_id=str(row["id"]),
            timestamp=aware(parse_iso(row["date"])),
            card_ref=str(row.get("card1Number") or "").strip(),
            vehicle=_clean(row.get("personName") or row.get("transportName") or row.get("recipientName")),
            fuel=row.get("fuelName") or "",
            liters=value / divisor if value is not None else None,
            station=row.get("azsName") or "",
            raw=row,
        )


class PetrolineLegacyProvider(BaseProvider):
    """Старий API Petroline (запасний варіант)."""

    name = "petroline"
    URL = "http://fms.petroline.in.ua:8055/api/TransactionsGet"

    def fetch(self, date_from: date, date_to: date) -> list[NormalizedTx]:
        divisor = _divisor()
        # Пароль користувача в системі у форматі SHA256
        pw = os.environ["PETROLINE_PASS"].strip()
        if len(pw) != 64:
            pw = hashlib.sha256(pw.encode("utf-8")).hexdigest()

        body = {
            "login": os.environ["PETROLINE_LOGIN"].strip(),
            "pass": pw,
            "datestart": f"{date_from:%d.%m.%Y} 00:00:00",
            "datefinish": f"{date_to:%d.%m.%Y} 23:59:59",
        }
        resp = requests.post(self.URL, json=body, timeout=60)
        if not resp.ok:
            raise RuntimeError(f"Petroline HTTP {resp.status_code}: {resp.text[:500]}")
        payload = resp.json()
        if payload.get("RequestStatus") != 200:
            raise RuntimeError(
                f"Petroline: {payload.get('RequestStatus')} {payload.get('RequestDescription')}"
            )

        items = []
        for row in payload.get("Result") or []:
            ts = datetime.strptime(row["Date"], "%d.%m.%Y %H:%M:%S")
            liters = to_dec(row.get("Value"))
            items.append(
                NormalizedTx(
                    provider=self.name,
                    external_id=str(row["TransactionID"]),
                    timestamp=aware(ts),
                    card_ref=row.get("Card1") or "",
                    vehicle=_clean(row.get("PersonName")),
                    fuel=row.get("Fuel") or "",
                    liters=liters / divisor if liters is not None else None,
                    station=row.get("AZSname") or "",
                    raw=row,
                )
            )
        return items
