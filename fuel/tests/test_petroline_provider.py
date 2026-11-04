"""Адаптер Petroline на новому API (v2). HTTP підмінено: реальні API не викликаються."""

import json
from datetime import date, datetime
from datetime import timezone as dt_timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

import pytest

from fuel.models import CardStatus, Transaction
from fuel.providers.petroline import PetrolineProvider, period_bounds_utc
from fuel.providers.petroline_api import PetrolineClient, PetrolineError, credentials
from fuel.providers.petroline_cards import PetrolineCards
from fuel.services import save_transactions

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "petroline_v2_transactions.json").read_text(encoding="utf-8")
)


@pytest.fixture(autouse=True)
def petroline_env(monkeypatch):
    for name in ("PETROLINE_V2_LOGIN", "PETROLINE_V2_PASSWORD", "PETROLINE_API", "PETROLINE_VALUE_DIVISOR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PETROLINE_LOGIN", "test-login")
    monkeypatch.setenv("PETROLINE_PASS", "test-password")


def response(payload, status=200):
    resp = mock.Mock()
    resp.ok = status < 400
    resp.status_code = status
    resp.json.return_value = payload
    return resp


def fake_session(pages: list, token_status=200):
    """Сесія, що відповідає токеном, а далі сторінками зі списку `pages`."""
    session = mock.Mock()
    session.headers = {}
    session.post.side_effect = [response({"token": "jwt"}, token_status)] + [response(p) for p in pages]
    return session


def provider_with(pages: list) -> tuple[PetrolineProvider, mock.Mock]:
    session = fake_session(pages)
    return PetrolineProvider(client=PetrolineClient(session=session)), session


def test_fetch_normalizes_records():
    provider, _ = provider_with([FIXTURE])

    items = provider.fetch(date(2026, 11, 1), date(2026, 11, 2))

    assert [it.external_id for it in items] == ["9000001", "9000002", "9000003"]
    first = items[0]
    assert first.provider == "petroline"
    assert first.timestamp == datetime(2026, 11, 1, 4, 56, 39, 30000, tzinfo=dt_timezone.utc)
    assert first.liters == Decimal("150.03")
    assert first.card_ref == "0000AAAA01"
    assert first.fuel == "ДП"
    assert first.station == "АЗС Тестова 1"
    assert first.raw["card1Id"] == 101


def test_vehicle_text_is_cleaned_and_falls_back_to_transport():
    provider, _ = provider_with([FIXTURE])

    items = provider.fetch(date(2026, 11, 1), date(2026, 11, 2))

    assert items[1].vehicle == "Навантажувач 7"
    assert items[2].vehicle == "FORD AA0000AA"


def test_request_body_has_kyiv_day_bounds_in_utc_and_token():
    provider, session = provider_with([FIXTURE])

    provider.fetch(date(2026, 11, 1), date(2026, 11, 2))

    auth_call, list_call = session.post.call_args_list
    assert auth_call.kwargs["json"] == {"login": "test-login", "password": "test-password"}
    assert list_call.args[0].endswith("/api/TrkTransactions/v2/list")
    body = list_call.kwargs["json"]
    # Київ у листопаді — UTC+2
    assert body["filter"]["date"] == {"start": "2026-10-31T22:00:00.000Z", "end": "2026-11-02T21:59:59.000Z"}
    assert body["loadOption"]["skip"] == 0
    assert session.headers["Authorization"] == "Bearer jwt"


def test_fetch_reads_all_pages():
    page_size = 2
    session = fake_session([FIXTURE[:2], FIXTURE[2:]])
    client = PetrolineClient(session=session)

    rows = client.post_paged("/api/TrkTransactions/v2/list", lambda s, t: {"skip": s, "take": t}, page_size)

    assert [r["id"] for r in rows] == [9000001, 9000002, 9000003]
    skips = [c.kwargs["json"]["skip"] for c in session.post.call_args_list[1:]]
    assert skips == [0, 2]


def test_auth_error_raises_petroline_error():
    client = PetrolineClient(session=fake_session([], token_status=401))

    with pytest.raises(PetrolineError, match="HTTP 401"):
        PetrolineProvider(client=client).fetch(date(2026, 11, 1), date(2026, 11, 1))


def test_sha256_password_is_rejected(monkeypatch):
    monkeypatch.setenv("PETROLINE_PASS", "a" * 64)

    with pytest.raises(PetrolineError, match="SHA256"):
        credentials()


def test_v2_credentials_take_priority(monkeypatch):
    monkeypatch.setenv("PETROLINE_V2_LOGIN", "v2-login")
    monkeypatch.setenv("PETROLINE_V2_PASSWORD", "v2-password")

    assert credentials() == ("v2-login", "v2-password")


def test_legacy_switch_uses_old_api(monkeypatch):
    monkeypatch.setenv("PETROLINE_API", "legacy")
    with mock.patch("fuel.providers.petroline.PetrolineLegacyProvider.fetch", return_value=[]) as legacy:
        assert PetrolineProvider(client=mock.Mock()).fetch(date(2026, 11, 1), date(2026, 11, 1)) == []
    legacy.assert_called_once()


def test_period_bounds_cover_whole_kyiv_days():
    assert period_bounds_utc(date(2026, 7, 1), date(2026, 7, 1)) == (
        "2026-06-30T21:00:00.000Z",
        "2026-07-01T20:59:59.000Z",
    )


def test_cards_use_shared_client():
    cards = [
        {
            "id": 1,
            "number": "0000AAAA01",
            "extNumber": "010001",
            "status": 3,
            "typeId": 1,
            "personId": 501,
            "personName": "AA0000AA | Тест  Тестович ",
        }
    ]
    client = PetrolineClient(session=fake_session([cards]))

    rows = PetrolineCards(client=client).list_cards()

    assert rows == [
        {
            "number": "0000AAAA01",
            "ext_number": "010001",
            "external_id": "1",
            "api_status": 3,
            "status": CardStatus.ACTIVE.value,
            "card_type": 1,
            "person_id": "501",
            "transport_id": "",
            "label": "AA0000AA | Тест Тестович",
            "vehicle": "AA0000AA",
            "driver_name": "Тест Тестович",
        }
    ]


def test_water_transactions_are_skipped():
    water = {**FIXTURE[0], "id": 9000099, "fuelId": 17, "fuelName": "Вода"}
    provider, _ = provider_with([[*FIXTURE, water]])

    items = provider.fetch(date(2026, 11, 1), date(2026, 11, 2))

    assert "9000099" not in [it.external_id for it in items]
    assert len(items) == 3


@pytest.mark.django_db
def test_resync_updates_without_duplicates():
    provider, _ = provider_with([FIXTURE])
    items = provider.fetch(date(2026, 11, 1), date(2026, 11, 2))

    assert save_transactions(items) == (3, 0)
    assert save_transactions(items) == (0, 3)
    assert Transaction.objects.filter(provider="petroline").count() == 3
    tx = Transaction.objects.get(provider="petroline", external_id="9000001")
    assert tx.card.number == "0000AAAA01"
    assert tx.liters == Decimal("150.030")


KEY_RECORD = {
    "id": 6001,
    "idString": "6001",
    "number": "0000AAAA01",
    "extNumber": "010001",
    "personId": 501,
    "transportId": None,
    "personName": "Тест",
    "status": 3,
    "typeId": 1,
    "barrelId": None,
    "groupId": 10,
    "groupName": "О_Тест",
    "comment": None,
    "secondCardMode": 0,
    "secondCardId": None,
    "pin": None,
}


def test_update_card_sends_full_key_with_only_status_changed():
    session = fake_session([[KEY_RECORD]])
    session.put.return_value = response(
        {"value": "6001", "status": 200, "returnObject": None, "errors": None}
    )

    PetrolineCards(client=PetrolineClient(session=session)).update_card(
        "0000AAAA01", status=CardStatus.BLOCKED
    )

    find_body = session.post.call_args_list[1].kwargs["json"]
    assert find_body["where"]["operands"][0] == {
        "operatorType": 0,
        "operands": None,
        "field": "number",
        "filterType": 1,
        "value": "0000AAAA01",
    }
    put_call = session.put.call_args
    assert put_call.args[0].endswith("/api/Keys/update")
    assert put_call.kwargs["json"] == {
        "id": 6001,
        "groupId": 10,
        "personId": 501,
        "transportId": None,
        "status": 5,
        "typeId": 1,
        "number": "0000AAAA01",
        "barrelId": None,
        "extNumber": "010001",
        "pin": None,
        "comment": None,
        "secondCardMode": 0,
        "secondCardId": None,
    }


def test_update_card_unblock_maps_to_status_3():
    session = fake_session([[{**KEY_RECORD, "status": 5}]])
    session.put.return_value = response({"value": "6001", "status": 200, "errors": None})

    PetrolineCards(client=PetrolineClient(session=session)).update_card(
        "0000AAAA01", status=CardStatus.ACTIVE
    )

    assert session.put.call_args.kwargs["json"]["status"] == 3


def test_update_card_error_in_body_raises():
    session = fake_session([[KEY_RECORD]])
    session.put.return_value = response({"value": None, "status": 400, "errors": ["bad"]})

    with pytest.raises(PetrolineError, match="400"):
        PetrolineCards(client=PetrolineClient(session=session)).update_card(
            "0000AAAA01", status=CardStatus.BLOCKED
        )


def test_update_card_not_found_does_not_write():
    session = fake_session([[]])

    with pytest.raises(PetrolineError, match="не знайдено"):
        PetrolineCards(client=PetrolineClient(session=session)).update_card(
            "0000AAAA01", status=CardStatus.BLOCKED
        )
    session.put.assert_not_called()
