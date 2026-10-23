import os
from datetime import date, datetime, timedelta

import requests

from .base import BaseProvider, NormalizedTx, aware, to_dec


class UpgProvider(BaseProvider):
    name = "upg"
    URL = "https://paycontainer.upg.ua/api/fuel-card-history"

    def fetch(self, date_from: date, date_to: date) -> list[NormalizedTx]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {os.environ['UPG_TOKEN']}",
        }
        items, page = [], 1
        while True:
            resp = requests.post(
                self.URL,
                data={
                    "date_from": date_from.isoformat(),
                    "date_to": (date_to + timedelta(days=1)).isoformat(),  # як у вашому прикладі
                    "page": page,
                },
                headers=headers,
                timeout=60,
            )
            resp.raise_for_status()
            payload = resp.json()
            for row in payload.get("data", []):
                ts = datetime.strptime(f"{row['transaction_date']} {row['transaction_time']}", "%Y-%m-%d %H:%M:%S")
                items.append(NormalizedTx(
                    provider=self.name,
                    external_id=str(row["id"]),
                    timestamp=aware(ts),
                    card_ref=row.get("card_number") or "",
                    vehicle=row.get("card_holder") or "",
                    fuel=row.get("fuel") or "",
                    liters=to_dec(row.get("amount")),
                    price=to_dec(row.get("price")),
                    amount=to_dec(row.get("total")),
                    station=row.get("service_station") or "",
                    raw=row,
                ))
            meta = payload.get("meta") or {}
            if page >= (meta.get("last_page") or 1):
                break
            page += 1
        return items
