"""Адаптери транзакцій UPG і UkrNafta на знеособлених реальних відповідях API.

Фікстури `upg_transactions.json`, `ukrnafta_transactions.json` зроблені пробою
`scripts/probes/save_fixture.py` (2026-11-10). HTTP підмінено, мережа заблокована.
"""

import copy
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from fuel.providers import ukrnafta, upg
from fuel.providers.ukrnafta import UkrnaftaProvider
from fuel.providers.upg import UpgProvider

FIXTURES = Path(__file__).parent / "fixtures"
KYIV = ZoneInfo("Europe/Kyiv")


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, data, status_code: int = 200, text: str = ""):
        self._data = data
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = text or json.dumps(data, ensure_ascii=False)

    def json(self):
        return self._data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


# --- UPG ----------------------------------------------------------------------------------------


@pytest.fixture
def upg_env(monkeypatch):
    monkeypatch.setenv("UPG_LOGIN", " login ")
    monkeypatch.setenv("UPG_PASSWORD", "secret")


def fake_upg(monkeypatch, pages: list[dict], login=None):
    """Логін віддає токен, далі — сторінки історії по черзі. Повертає список запитів."""
    calls = []
    login = login or FakeResponse({"token": "T"})

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url == UpgProvider.LOGIN_URL:
            return login
        return FakeResponse(pages[kwargs["data"]["page"] - 1])

    monkeypatch.setattr(upg.requests, "post", post)
    return calls


def test_upg_maps_real_response(monkeypatch, upg_env):
    payload = load("upg_transactions.json")
    calls = fake_upg(monkeypatch, [payload])

    items = UpgProvider().fetch(date(2026, 11, 9), date(2026, 11, 9))

    assert len(items) == len(payload["data"]) == 3
    first, row = items[0], payload["data"][0]
    assert first.provider == "upg"
    assert first.external_id == str(row["id"])
    assert first.kind == "fuel"
    assert first.timestamp == datetime(2026, 11, 9, 13, 49, 38, tzinfo=KYIV)
    assert first.card_ref == row["card_number"]
    assert first.vehicle == row["card_holder"]
    assert first.fuel == "Бензин Аи-95Евро"
    assert (first.liters, first.price, first.amount) == (
        Decimal("29.5900"),
        Decimal("88.9000"),
        Decimal("2615.7500"),
    )
    assert first.station == row["service_station"]
    assert first.raw == row

    login_url, login_kwargs = calls[0]
    assert login_url == UpgProvider.LOGIN_URL
    assert login_kwargs["data"] == {"login": "login", "password": "secret"}
    _, history = calls[1]
    assert history["headers"]["Authorization"] == "Bearer T"
    # date_to у запиті — наступний день: API не включає кінцеву дату
    assert history["data"] == {"date_from": "2026-11-09", "date_to": "2026-11-10", "page": 1}


def test_upg_reads_all_pages(monkeypatch, upg_env):
    page1 = load("upg_transactions.json")
    page1["meta"]["last_page"] = 2
    page2 = copy.deepcopy(page1)
    page2["data"] = [{**page2["data"][0], "id": 1}]
    calls = fake_upg(monkeypatch, [page1, page2])

    items = UpgProvider().fetch(date(2026, 11, 9), date(2026, 11, 9))

    assert len(items) == 4
    assert [c[1]["data"]["page"] for c in calls[1:]] == [1, 2]


@pytest.mark.parametrize(
    "response, message",
    [
        (FakeResponse({}, status_code=401, text="bad credentials"), "UPG login HTTP 401"),
        (FakeResponse({"message": "ok"}), "немає token"),
    ],
)
def test_upg_login_errors(monkeypatch, upg_env, response, message):
    fake_upg(monkeypatch, [], login=response)
    with pytest.raises(RuntimeError, match=message):
        UpgProvider().fetch(date(2026, 11, 9), date(2026, 11, 9))


def test_upg_history_http_error(monkeypatch, upg_env):
    def post(url, **kwargs):
        if url == UpgProvider.LOGIN_URL:
            return FakeResponse({"token": "T"})
        return FakeResponse({}, status_code=500, text="boom")

    monkeypatch.setattr(upg.requests, "post", post)
    with pytest.raises(RuntimeError, match="UPG HTTP 500"):
        UpgProvider().fetch(date(2026, 11, 9), date(2026, 11, 9))


# --- UkrNafta -----------------------------------------------------------------------------------


@pytest.fixture
def ukrnafta_env(monkeypatch):
    monkeypatch.setenv("UKRNAFTA_HOLDER_ID", "77")
    monkeypatch.setenv("UKRNAFTA_TOKEN", "T")


def fake_ukrnafta(monkeypatch, payload) -> list:
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        return payload if isinstance(payload, FakeResponse) else FakeResponse(payload)

    monkeypatch.setattr(ukrnafta.requests, "get", get)
    return calls


def by_id(items) -> dict:
    return {i.external_id: i for i in items}


def test_ukrnafta_maps_fuel_from_real_response(monkeypatch, ukrnafta_env):
    calls = fake_ukrnafta(monkeypatch, load("ukrnafta_transactions.json"))

    tx = by_id(UkrnaftaProvider().fetch(date(2026, 10, 10), date(2026, 11, 9)))["22014257"]

    assert tx.kind == "fuel"
    assert tx.timestamp == datetime(2026, 10, 30, 7, 45, 2, tzinfo=KYIV)
    assert tx.card_ref == "CARDID-002"
    assert tx.fuel == "ДП"  # назва з products, не purseRemark
    # API віддає списання зі знаком «−»; у нас літри й сума додатні
    assert (tx.liters, tx.amount, tx.price) == (Decimal("132.27"), Decimal("13610.58"), Decimal("102.9"))
    assert tx.station.startswith("АЗС")
    assert tx.canceled is False

    params = calls[0][1]["params"]
    assert (params["fromDate"], params["toDate"], params["holderId"]) == ("2026-10-10", "2026-11-10", "77")
    assert params["with_cancelation"] == "true"


def test_ukrnafta_maps_topup(monkeypatch, ukrnafta_env):
    fake_ukrnafta(monkeypatch, load("ukrnafta_transactions.json"))

    tx = by_id(UkrnaftaProvider().fetch(date(2026, 10, 10), date(2026, 11, 9)))["22073979"]

    assert tx.kind == "topup"
    assert tx.amount == Decimal("410.03")
    assert tx.card_ref == ""
    assert tx.liters is None
    # частки секунди з трьома цифрами (…25.763) розбираються і на Python 3.10
    assert tx.timestamp == datetime(2026, 10, 31, 5, 34, 25, 763000, tzinfo=KYIV)


def test_ukrnafta_zero_fuel_row_is_kept(monkeypatch, ukrnafta_env):
    """Реальний рядок зі списанням 0 л / 0 грн: зберігається як є, без помилок."""
    fake_ukrnafta(monkeypatch, load("ukrnafta_transactions.json"))

    tx = by_id(UkrnaftaProvider().fetch(date(2026, 10, 10), date(2026, 11, 9)))["22511116"]

    assert (tx.liters, tx.amount) == (Decimal("0"), Decimal("0"))


def test_ukrnafta_skips_other_types_and_keeps_cancellation(monkeypatch, ukrnafta_env):
    payload = load("ukrnafta_transactions.json")
    fuel_row = next(r for r in payload["transactions"] if r["typeId"] == 302 and r["amount"])
    canceled = {**fuel_row, "transactionId": 1, "canceled": True}
    other = {**fuel_row, "transactionId": 2, "typeId": 999}
    without_products = {**fuel_row, "transactionId": 3, "products": []}
    payload["transactions"] = [canceled, other, without_products]
    fake_ukrnafta(monkeypatch, payload)

    items = by_id(UkrnaftaProvider().fetch(date(2026, 10, 10), date(2026, 11, 9)))

    assert set(items) == {"1", "3"}
    assert items["1"].canceled is True
    assert items["3"].fuel == fuel_row["purseRemark"]  # без products — назва гаманця
    assert items["3"].price is None


def test_ukrnafta_error_code_raises(monkeypatch, ukrnafta_env):
    fake_ukrnafta(monkeypatch, {"errorCode": 3, "errorDescription": "Невірний токен", "transactions": []})
    with pytest.raises(RuntimeError, match="Невірний токен"):
        UkrnaftaProvider().fetch(date(2026, 11, 9), date(2026, 11, 9))


def test_ukrnafta_http_error_raises(monkeypatch, ukrnafta_env):
    fake_ukrnafta(monkeypatch, FakeResponse({}, status_code=403))
    with pytest.raises(RuntimeError, match="HTTP 403"):
        UkrnaftaProvider().fetch(date(2026, 11, 9), date(2026, 11, 9))


def test_ukrnafta_single_short_page_is_one_request(monkeypatch, ukrnafta_env):
    """Реальна відповідь: 4 рядки менше за розмір сторінки — більше сторінок не просимо."""
    calls = fake_ukrnafta(monkeypatch, load("ukrnafta_transactions.json"))

    UkrnaftaProvider().fetch(date(2026, 10, 10), date(2026, 11, 9))

    assert len(calls) == 1
    assert (calls[0][1]["params"]["page"], calls[0][1]["params"]["limit"]) == (0, ukrnafta.PAGE_SIZE)


# --- UkrNafta, пагінація (формат перевірено пробою 2026-11-11: page з 0, countOfRows — усього) ---


def fuel_rows(count: int, start: int = 1) -> list[dict]:
    base = next(r for r in load("ukrnafta_transactions.json")["transactions"] if r["typeId"] == 302)
    return [{**base, "transactionId": start + i} for i in range(count)]


def fake_ukrnafta_pages(monkeypatch, pages: list, total: int | None) -> list[int]:
    """Сторінки за номером `page`; за межами списку — порожня сторінка, як у реального API."""
    requested = []

    def get(url, **kwargs):
        page = kwargs["params"]["page"]
        requested.append(page)
        item = pages[page] if page < len(pages) else []
        if isinstance(item, FakeResponse):
            return item
        return FakeResponse({"errorCode": 0, "countOfRows": total, "transactions": item})

    monkeypatch.setattr(ukrnafta.requests, "get", get)
    monkeypatch.setattr(ukrnafta, "PAGE_SIZE", 2)
    return requested


def ids_of(items) -> list[str]:
    return [i.external_id for i in items]


def test_ukrnafta_reads_all_pages_until_short_page(monkeypatch, ukrnafta_env):
    rows = fuel_rows(5)
    requested = fake_ukrnafta_pages(monkeypatch, [rows[0:2], rows[2:4], rows[4:5]], total=5)

    items = UkrnaftaProvider().fetch(date(2026, 11, 1), date(2026, 11, 9))

    assert ids_of(items) == ["1", "2", "3", "4", "5"]
    assert requested == [0, 1, 2]


def test_ukrnafta_stops_when_count_of_rows_reached(monkeypatch, ukrnafta_env):
    """Остання сторінка повна: зупиняємось за countOfRows, без зайвого запиту порожньої сторінки."""
    rows = fuel_rows(4)
    requested = fake_ukrnafta_pages(monkeypatch, [rows[0:2], rows[2:4]], total=4)

    assert len(UkrnaftaProvider().fetch(date(2026, 11, 1), date(2026, 11, 9))) == 4
    assert requested == [0, 1]


def test_ukrnafta_stops_on_empty_page_without_count(monkeypatch, ukrnafta_env):
    rows = fuel_rows(4)
    requested = fake_ukrnafta_pages(monkeypatch, [rows[0:2], rows[2:4]], total=None)

    assert len(UkrnaftaProvider().fetch(date(2026, 11, 1), date(2026, 11, 9))) == 4
    assert requested == [0, 1, 2]


def test_ukrnafta_drops_duplicates_between_pages(monkeypatch, ukrnafta_env):
    """Нова транзакція між запитами зсуває сторінки: повтор на межі відкидається."""
    rows = fuel_rows(4)
    requested = fake_ukrnafta_pages(monkeypatch, [rows[0:2], rows[1:3], rows[3:4]], total=4)

    assert ids_of(UkrnaftaProvider().fetch(date(2026, 11, 1), date(2026, 11, 9))) == ["1", "2", "3", "4"]
    assert requested == [0, 1, 2]


def test_ukrnafta_error_on_later_page_fails_whole_run(monkeypatch, ukrnafta_env):
    """Збій другої сторінки — збій усього запуску: частину періоду мовчки не зберігаємо."""
    rows = fuel_rows(4)
    fake_ukrnafta_pages(monkeypatch, [rows[0:2], FakeResponse({}, status_code=500)], total=4)

    with pytest.raises(RuntimeError, match="HTTP 500"):
        UkrnaftaProvider().fetch(date(2026, 11, 1), date(2026, 11, 9))


def test_ukrnafta_warns_when_fewer_rows_than_count(monkeypatch, ukrnafta_env, caplog):
    fake_ukrnafta_pages(monkeypatch, [fuel_rows(1)], total=3)

    with caplog.at_level("WARNING", logger="fuel.providers.ukrnafta"):
        assert len(UkrnaftaProvider().fetch(date(2026, 11, 1), date(2026, 11, 9))) == 1

    assert "отримано 1 транзакцій з 3" in caplog.text


def test_ukrnafta_page_limit_guard(monkeypatch, ukrnafta_env):
    """API, що без кінця віддає повні сторінки, не зациклює синхронізацію."""
    monkeypatch.setattr(ukrnafta, "MAX_PAGES", 3)
    counter = iter(range(1, 1000))

    def get(url, **kwargs):
        batch = fuel_rows(2, start=next(counter) * 10)
        return FakeResponse({"errorCode": 0, "countOfRows": None, "transactions": batch})

    monkeypatch.setattr(ukrnafta.requests, "get", get)
    monkeypatch.setattr(ukrnafta, "PAGE_SIZE", 2)
    with pytest.raises(RuntimeError, match="понад 3 сторінок"):
        UkrnaftaProvider().fetch(date(2026, 11, 1), date(2026, 11, 9))
