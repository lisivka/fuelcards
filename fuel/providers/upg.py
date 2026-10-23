import os
from datetime import date, datetime, timedelta

import requests

from .base import BaseProvider, NormalizedTx, aware, to_dec

BASE = "https://paycontainer.upg.ua/api"


class UpgProvider(BaseProvider):
    name = "upg"
    LOGIN_URL = f"{BASE}/login"
    URL = f"{BASE}/fuel-card-history"

    def _login(self) -> str:
        resp = requests.post(
            self.LOGIN_URL,
            data={
                "login": os.environ["UPG_LOGIN"].strip(),
                "password": os.environ["UPG_PASSWORD"],
            },
            headers={"Accept": "application/json"},
            timeout=60,
        )
        if not resp.ok:
            raise RuntimeError(f"UPG login HTTP {resp.status_code}: {resp.text[:500]}")
        token = resp.json().get("token")
        if not token:
            raise RuntimeError(f"UPG login: у відповіді немає token: {resp.text[:300]}")
        return token

    def fetch(self, date_from: date, date_to: date) -> list[NormalizedTx]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._login()}",
        }
        items, page = [], 1
        while True:
            resp = requests.post(
                self.URL,
                data={
                    "date_from": date_from.isoformat(),
                    "date_to": (date_to + timedelta(days=1)).isoformat(),
                    "page": page,
                },
                headers=headers,
                timeout=60,
            )
            if not resp.ok:
                raise RuntimeError(f"UPG HTTP {resp.status_code}: {resp.text[:500]}")
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