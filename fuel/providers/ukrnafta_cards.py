import os

import requests

BASE = "https://naftacards.ukrnafta.com/public-api"


class UkrnaftaError(RuntimeError):
    pass


class UkrnaftaCards:
    """Робота з картками UkrNafta: список зі статусами та оновлення картки."""

    def _headers(self):
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {os.environ['UKRNAFTA_TOKEN']}",
        }

    @staticmethod
    def _check(resp) -> dict:
        if not resp.ok:
            raise UkrnaftaError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if data.get("errorCode") not in (0, None):
            raise UkrnaftaError(f"{data.get('errorCode')}: {data.get('errorDescription')}")
        return data

    def list_cards(self) -> list[dict]:
        account = os.environ["UKRNAFTA_ACCOUNT_ID"].strip()
        resp = requests.get(
            f"{BASE}/accounts/{account}/cards",
            params={"lang": "uk"}, headers=self._headers(), timeout=60,
        )
        return self._check(resp).get("cards") or []

    def update_card(self, card_number: str, *, status=None, vin=None, last_name=None, first_name=None) -> None:
        """Надсилає лише передані поля."""
        payload = {}
        if status is not None:
            payload["status"] = int(status)
        if vin is not None:
            payload["vin"] = vin
        info = {}
        if last_name is not None:
            info["lastName"] = last_name
        if first_name is not None:
            info["firstName"] = first_name
        if info:
            payload["cardInfo"] = info
        resp = requests.post(
            f"{BASE}/cards/{card_number}/update",
            params={"lang": "uk"}, json=payload, headers=self._headers(), timeout=60,
        )
        self._check(resp)
