"""Адаптер карток UkrNafta: список, оновлення (тіло запиту), помилки. HTTP підмінено."""

import pytest

from fuel.providers import ukrnafta_cards
from fuel.providers.ukrnafta_cards import UkrnaftaCards, UkrnaftaError


class FakeResponse:
    def __init__(self, data, status_code: int = 200, text: str = ""):
        self._data = data
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = text

    def json(self):
        return self._data


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("UKRNAFTA_TOKEN", " token ")
    monkeypatch.setenv("UKRNAFTA_ACCOUNT_ID", " 42 ")


@pytest.fixture
def calls(monkeypatch):
    """Записує запити і віддає відповідь із `calls.response`."""

    class Calls(list):
        response = FakeResponse({"errorCode": 0})

    recorded = Calls()

    def fake(method):
        def call(url, **kwargs):
            recorded.append((method, url, kwargs))
            return recorded.response

        return call

    monkeypatch.setattr(ukrnafta_cards.requests, "get", fake("GET"))
    monkeypatch.setattr(ukrnafta_cards.requests, "post", fake("POST"))
    return recorded


def test_list_cards_uses_account_and_token(calls):
    calls.response = FakeResponse({"errorCode": 0, "cards": [{"cardId": "1"}]})

    assert UkrnaftaCards().list_cards() == [{"cardId": "1"}]

    method, url, kwargs = calls[0]
    assert (method, url) == ("GET", f"{ukrnafta_cards.BASE}/accounts/42/cards")
    assert kwargs["headers"]["Authorization"] == "Bearer token"


def test_list_cards_empty_body_gives_empty_list(calls):
    calls.response = FakeResponse({"errorCode": 0, "cards": None})
    assert UkrnaftaCards().list_cards() == []


def test_update_card_sends_only_given_fields(calls):
    UkrnaftaCards().update_card("0001", status=1)
    UkrnaftaCards().update_card("0001", vin="AA1111AA", last_name="Іваненко", first_name="Іван")

    (_, url, first), (_, _, second) = calls
    assert url == f"{ukrnafta_cards.BASE}/cards/0001/update"
    assert first["json"] == {"status": 1}
    assert second["json"] == {"vin": "AA1111AA", "cardInfo": {"lastName": "Іваненко", "firstName": "Іван"}}


def test_update_card_allows_clearing_vin(calls):
    UkrnaftaCards().update_card("0001", vin="")
    assert calls[0][2]["json"] == {"vin": ""}


def test_http_error_raises(calls):
    calls.response = FakeResponse({}, status_code=403, text="Forbidden")
    with pytest.raises(UkrnaftaError, match="HTTP 403"):
        UkrnaftaCards().update_card("0001", status=0)


def test_error_code_in_body_raises(calls):
    calls.response = FakeResponse({"errorCode": 12, "errorDescription": "Картку не знайдено"})
    with pytest.raises(UkrnaftaError, match="Картку не знайдено"):
        UkrnaftaCards().list_cards()


def test_missing_token_raises_before_request(calls, monkeypatch):
    monkeypatch.setenv("UKRNAFTA_TOKEN", "  ")
    with pytest.raises(UkrnaftaError, match="UKRNAFTA_TOKEN"):
        UkrnaftaCards().list_cards()
    assert calls == []
