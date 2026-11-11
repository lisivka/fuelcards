"""Транзакції UkrNafta: `GET /public-api/common/transactions` з пагінацією.

Пагінація (перевірено пробою 2026-11-11): `page` з 0, `limit` — розмір сторінки, `countOfRows` — загальна
кількість записів за період (однакова на кожній сторінці); сторінка за межами даних — порожня; разом
сторінки дають той самий набір і порядок, що й один великий запит. Читаємо до порожньої/неповної сторінки
або поки не зібрано `countOfRows`. Збій будь-якої сторінки — збій усього запуску UkrNafta: краще не
зберегти нічого, ніж мовчки загубити частину періоду.
"""

import logging
import os
from datetime import date, timedelta

import requests

from .base import BaseProvider, NormalizedTx, aware, parse_iso, to_dec

log = logging.getLogger(__name__)

TYPE_FUEL = 302
TYPE_TOPUP = 214
PAGE_SIZE = 1000
MAX_PAGES = 200  # запобіжник від нескінченного циклу (200 000 записів за період)


class UkrnaftaProvider(BaseProvider):
    name = "ukrnafta"
    URL = "https://naftacards.ukrnafta.com/public-api/common/transactions"

    def fetch(self, date_from: date, date_to: date) -> list[NormalizedTx]:
        items = []
        for row in self._fetch_rows(date_from, date_to):
            tx = self._normalize(row)
            if tx is not None:
                items.append(tx)
        return items

    def _fetch_rows(self, date_from: date, date_to: date) -> list[dict]:
        """Усі записи за період, сторінка за сторінкою; повтори за transactionId відкидаються."""
        rows: list[dict] = []
        seen: set[str] = set()
        total = None
        for page in range(MAX_PAGES):
            payload = self._fetch_page(date_from, date_to, page)
            total = payload.get("countOfRows")
            batch = payload.get("transactions") or []
            for row in batch:
                key = str(row.get("transactionId"))
                if key not in seen:
                    seen.add(key)
                    rows.append(row)
            if len(batch) < PAGE_SIZE or (isinstance(total, int) and len(rows) >= total):
                break
        else:
            raise RuntimeError(f"UkrNafta: понад {MAX_PAGES} сторінок транзакцій за {date_from}..{date_to}")
        if isinstance(total, int) and len(rows) < total:
            log.warning(
                "UkrNafta: за %s..%s отримано %s транзакцій з %s", date_from, date_to, len(rows), total
            )
        if page:
            log.info("UkrNafta: %s транзакцій за %s сторінок", len(rows), page + 1)
        return rows

    def _fetch_page(self, date_from: date, date_to: date, page: int) -> dict:
        resp = requests.get(
            self.URL,
            params={
                "fromDate": date_from.isoformat(),
                "toDate": (date_to + timedelta(days=1)).isoformat(),
                "holderId": os.environ["UKRNAFTA_HOLDER_ID"],
                "page": page,
                "limit": PAGE_SIZE,
                "operation_kind": 0,
                "with_cancelation": "true",
                "lang": "uk",
            },
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {os.environ['UKRNAFTA_TOKEN']}",
            },
            timeout=60,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("errorCode") not in (0, None):
            raise RuntimeError(f"UkrNafta: {payload.get('errorCode')} {payload.get('errorDescription')}")
        return payload

    def _normalize(self, row: dict) -> NormalizedTx | None:
        type_id = row.get("typeId")
        if type_id not in (TYPE_FUEL, TYPE_TOPUP):
            return None
        ts = aware(parse_iso(row["terminalDate"]))
        amount = to_dec(row.get("amount"))
        if type_id == TYPE_TOPUP:
            return NormalizedTx(
                provider=self.name,
                external_id=str(row["transactionId"]),
                timestamp=ts,
                kind="topup",
                amount=amount,
                station=row.get("terminalName") or "",
                canceled=bool(row.get("canceled")),
                raw=row,
            )
        product = (row.get("products") or [{}])[0]
        qty = to_dec(row.get("quantity"))
        return NormalizedTx(
            provider=self.name,
            external_id=str(row["transactionId"]),
            timestamp=ts,
            card_ref=row.get("cardId") or "",
            fuel=product.get("name") or row.get("purseRemark") or "",
            liters=abs(qty) if qty is not None else None,
            price=to_dec(product.get("price")),
            amount=abs(amount) if amount is not None else None,
            station=row.get("terminalName") or "",
            canceled=bool(row.get("canceled")),
            raw=row,
        )
