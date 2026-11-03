"""Спільний клієнт нового API Petroline (https://apiv2.petroline.in.ua).

Авторизація: `POST /api/Auth/token` (логін і звичайний пароль, не SHA256) → JWT у заголовку Bearer.
Списки читаються сторінками через `skip`/`take`.
"""

import os
import re
from collections.abc import Callable

import requests

BASE = "https://apiv2.petroline.in.ua"
TIMEOUT = 60
PAGE_SIZE = 100
MAX_RECORDS = 50_000  # запобіжник від нескінченного циклу пагінації

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


class PetrolineError(RuntimeError):
    """Помилка роботи з API Petroline."""


def _env(*names: str) -> str:
    """Перше непорожнє значення серед змінних оточення."""
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def credentials() -> tuple[str, str]:
    """Логін і пароль: спершу PETROLINE_V2_*, якщо порожні — PETROLINE_LOGIN / PETROLINE_PASS."""
    login = _env("PETROLINE_V2_LOGIN", "PETROLINE_LOGIN")
    password = _env("PETROLINE_V2_PASSWORD", "PETROLINE_PASS")
    if not login or not password:
        raise PetrolineError("Не задано логін/пароль Petroline у .env")
    if _SHA256.match(password):
        raise PetrolineError(
            "Для нового API Petroline потрібен звичайний пароль, а не SHA256: задайте PETROLINE_V2_PASSWORD у .env"
        )
    return login, password


def as_list(data) -> list:
    """Відповідь-список може прийти як масив або як об'єкт зі списком усередині."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return next((v for v in data.values() if isinstance(v, list)), [])
    return []


class PetrolineClient:
    """Сесія з токеном і пагінований POST-запит до списків."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self._authorized = False

    def login(self) -> None:
        login, password = credentials()
        resp = self.session.post(
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
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self._authorized = True

    def post(self, path: str, body: dict):
        if not self._authorized:
            self.login()
        resp = self.session.post(f"{BASE}{path}", json=body, timeout=TIMEOUT)
        if not resp.ok:
            raise PetrolineError(f"Petroline {path}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise PetrolineError(f"Petroline {path}: некоректна відповідь") from None

    def post_paged(
        self, path: str, make_body: Callable[[int, int], dict], page_size: int = PAGE_SIZE
    ) -> list[dict]:
        """Читає всі сторінки: make_body(skip, take) будує тіло запиту для кожної сторінки."""
        records: list[dict] = []
        while len(records) < MAX_RECORDS:
            page = as_list(self.post(path, make_body(len(records), page_size)))
            records.extend(page)
            if len(page) < page_size:
                break
        return records
