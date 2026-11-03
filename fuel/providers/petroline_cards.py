"""Клієнт карток Petroline (apiv2.petroline.in.ua). Етап А: лише читання списку карток."""
import os
import re

import requests

from ..models import CardStatus

BASE = "https://apiv2.petroline.in.ua"
TIMEOUT = 60
PAGE_SIZE = 100
MAX_CARDS = 5000  # запобіжник від нескінченного циклу пагінації

# Статуси Petroline -> наші (CardStatus). Невідомі коди не вгадуємо: None.
STATUS_FROM_API = {3: CardStatus.ACTIVE.value, 5: CardStatus.BLOCKED.value}
TYPE_VEHICLE = 5  # «Автомобільна»: у картки поле Транспорт; в інших типів поле Водій

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class PetrolineError(RuntimeError):
    """Помилка роботи з API Petroline."""


class PetrolineCards:
    """Список карток Petroline: номери, id, статус і назва водія/авто."""

    def _token(self) -> str:
        login = (os.environ.get("PETROLINE_LOGIN") or os.environ.get("PETROLINE_LOGIN") or "").strip()
        password = (os.environ.get("PETROLINE_PASS") or os.environ.get("PETROLINE_PASS") or "").strip()
        if not login or not password:
            raise PetrolineError("Не задано логін/пароль Petroline у .env")
        if _SHA256.match(password):
            raise PetrolineError(
                "Для нового API Petroline потрібен звичайний пароль: задайте PETROLINE_PASS у .env"
            )
        resp = requests.post(
            f"{BASE}/api/Auth/token", json={"login": login, "password": password}, timeout=TIMEOUT
        )
        if not resp.ok:
            raise PetrolineError(f"Авторизація Petroline: HTTP {resp.status_code}")
        try:
            token = (resp.json() or {}).get("token")
        except ValueError:
            token = None
        if not token:
            raise PetrolineError("Petroline не повернув токен")
        return token

    def list_cards(self) -> list[dict]:
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        records: list = []
        while len(records) < MAX_CARDS:
            body = {"orderBy": [{"field": "id", "order": 0}], "skip": len(records), "take": PAGE_SIZE}
            resp = requests.post(f"{BASE}/api/Keys/list", json=body, headers=headers, timeout=TIMEOUT)
            if not resp.ok:
                raise PetrolineError(f"Keys/list: HTTP {resp.status_code}")
            try:
                page = resp.json()
            except ValueError:
                raise PetrolineError("Keys/list: некоректна відповідь") from None
            if isinstance(page, dict):
                page = next((v for v in page.values() if isinstance(v, list)), [])
            records.extend(page)
            if len(page) < PAGE_SIZE:
                break
        return [row for row in map(self._normalize, records) if row]

    @staticmethod
    def _normalize(rec: dict) -> dict | None:
        """Лише потрібні поля; PIN та інше чутливе далі не передається."""
        number = str(rec.get("number") or "").strip()
        if not number:
            return None
        if rec.get("typeId") == TYPE_VEHICLE:
            names = (rec.get("transportName"), rec.get("transportNumber"), rec.get("personName"))
        else:
            names = (rec.get("personName"), rec.get("transportName"), rec.get("transportNumber"))
        label = next((" ".join(str(n).split()) for n in names if n and str(n).strip()), "")
        return {
            "number": number,
            "ext_number": str(rec.get("extNumber") or "").strip(),
            "external_id": str(rec.get("id") or ""),
            "api_status": rec.get("status"),
            "status": STATUS_FROM_API.get(rec.get("status")),
            "label": label,
        }