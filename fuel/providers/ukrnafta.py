import os
from datetime import date, datetime, timedelta

import requests

from .base import BaseProvider, NormalizedTx, aware, to_dec, parse_iso

TYPE_FUEL = 302
TYPE_TOPUP = 214


class UkrnaftaProvider(BaseProvider):
    name = "ukrnafta"
    URL = "https://naftacards.ukrnafta.com/public-api/common/transactions"

    def fetch(self, date_from: date, date_to: date) -> list[NormalizedTx]:
        resp = requests.get(
            self.URL,
            params={
                "fromDate": date_from.isoformat(),
                "toDate": (date_to + timedelta(days=1)).isoformat(),
                "holderId": os.environ["UKRNAFTA_HOLDER_ID"],
                "page": 0,
                "limit": 1000,
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

        items = []
        for row in payload.get("transactions", []):
            type_id = row.get("typeId")
            if type_id not in (TYPE_FUEL, TYPE_TOPUP):
                continue
            ts = aware(parse_iso(row["terminalDate"]))
            amount = to_dec(row.get("amount"))
            if type_id == TYPE_TOPUP:
                items.append(NormalizedTx(
                    provider=self.name, external_id=str(row["transactionId"]), timestamp=ts,
                    kind="topup", amount=amount, station=row.get("terminalName") or "",
                    canceled=bool(row.get("canceled")), raw=row,
                ))
                continue
            product = (row.get("products") or [{}])[0]
            qty = to_dec(row.get("quantity"))
            items.append(NormalizedTx(
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
            ))
        return items
