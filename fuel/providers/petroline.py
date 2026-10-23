import hashlib
import os
import re
from datetime import date, datetime
from decimal import Decimal
from urllib.parse import urlencode

import requests

from .base import BaseProvider, NormalizedTx, aware, to_dec


class PetrolineProvider(BaseProvider):
    name = "petroline"
    URL = "http://fms.petroline.in.ua:8055/api/TransactionsGet"

    def fetch(self, date_from: date, date_to: date) -> list[NormalizedTx]:
        divisor = Decimal(os.environ.get("PETROLINE_VALUE_DIVISOR", "1000"))
        # Пароль користувача в системі в формате SHA256
        pw = os.environ["PETROLINE_PASS"].strip()
        if len(pw) != 64:
            pw = hashlib.sha256(pw.encode("utf-8")).hexdigest()


        body = {
            "login": os.environ["PETROLINE_LOGIN"].strip(),
            "pass": pw,
            "datestart": f"{date_from:%d.%m.%Y} 00:00:00",
            "datefinish": f"{date_to:%d.%m.%Y} 23:59:59",
        }
        # Як у вашому запиті: тіло "k=v&k=v" із заголовком application/json
        resp = requests.post(self.URL, json=body, timeout=60)
        if not resp.ok:
            raise RuntimeError(f"Petroline HTTP {resp.status_code}: {resp.text[:500]}")

        resp.raise_for_status()
        payload = resp.json()
        if payload.get("RequestStatus") != 200:
            raise RuntimeError(f"Petroline: {payload.get('RequestStatus')} {payload.get('RequestDescription')}")

        items = []
        for row in payload.get("Result") or []:
            ts = datetime.strptime(row["Date"], "%d.%m.%Y %H:%M:%S")
            liters = to_dec(row.get("Value"))
            items.append(NormalizedTx(
                provider=self.name,
                external_id=str(row["TransactionID"]),
                timestamp=aware(ts),
                card_ref=row.get("Card1") or "",
                vehicle=" ".join((row.get("PersonName") or "").split()),
                fuel=row.get("Fuel") or "",
                liters=liters / divisor if liters is not None else None,
                station=row.get("AZSname") or "",
                raw=row,
            ))
        return items
