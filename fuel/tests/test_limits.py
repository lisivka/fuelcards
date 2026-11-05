"""Ліміти карток на пальне (ЗАДАЧА-4а): розбір відповідей, скорочення, знімок при оновленні карток,
оновлення після розблокування, відображення. Відповіді API — знеособлені фікстури, мережа заблокована.
"""

import json
from pathlib import Path

import pytest
import requests
from django.urls import reverse

from fuel import card_service
from fuel import limits as lim
from fuel.models import AuditLog, Card, CardStatus, ProviderConfig
from fuel.providers import ukrnafta_cards
from fuel.providers.ukrnafta_cards import UkrnaftaCards, UkrnaftaError
from fuel.tests.conftest import NetworkBlocked

FIXTURES = Path(__file__).parent / "fixtures"
UN = json.loads((FIXTURES / "ukrnafta_limits.json").read_text(encoding="utf-8"))
PL = json.loads((FIXTURES / "petroline_person_limits.json").read_text(encoding="utf-8"))["person_limits"]
FUEL_PURSES = {p["purseId"]: p["remark"] for p in UN["purses"]["purses"] if p["isFuel"]}
CARD_ONE, CARD_TWO, CARD_EMPTY = "7921000000000001", "7921000000000002", "7921000000000003"


def un_limits(card_id: str) -> list[dict]:
    return UN["card_limits"][card_id]["limits"]


def pl_fuel_record(**fields) -> dict:
    """Запис PersonLimit на пальне (у реальних даних таких поки немає — беремо запис омивача і міняємо пальне)."""
    rec = dict(PL[1])
    rec.update(fuelId=1, fuel="ДП", **fields)
    return rec


# --- розбір і скорочення ------------------------------------------------------------------------


def test_all_ukrnafta_types_are_mapped():
    ids = {t["id"] for t in UN["limit_types"]["types"]}
    assert ids == set(lim.UKRNAFTA_TYPES)
    for t in UN["limit_types"]["types"]:
        period, money = lim.UKRNAFTA_TYPES[t["id"]]
        assert t["remark"].startswith("Гривні" if money else "Літри")


def test_ukrnafta_two_limits_short_and_fuels():
    limits = lim.ukrnafta_limits(un_limits(CARD_TWO), FUEL_PURSES)
    assert lim.short(limits) == "Д60 М60"
    assert limits[0]["fuels"] == ["ДП", "ДП ENERGY"]
    assert limits[1]["fuels"] == ["А-95", "А-95 ENERGY"]


def test_ukrnafta_money_once_and_unknown_type():
    raw = [
        {"limitType": 7, "limitAmount": 1500.5, "attachedPurses": [{"purseId": 7}]},
        {"limitType": 99, "limitAmount": 10, "attachedPurses": [{"purseId": 7}]},
    ]
    assert lim.short(lim.ukrnafta_limits(raw, FUEL_PURSES)) == "Р1500.5₴"


def test_ukrnafta_non_fuel_and_zero_limits_are_skipped():
    raw = [
        {"limitType": 4, "limitAmount": 100, "attachedPurses": [{"purseId": 20}]},  # лише мийка
        {"limitType": 3, "limitAmount": 0, "attachedPurses": [{"purseId": 7}]},
    ]
    assert lim.ukrnafta_limits(raw, FUEL_PURSES) == []
    assert lim.ukrnafta_limits(un_limits(CARD_EMPTY), FUEL_PURSES) == []


def test_same_limit_for_different_fuels_is_merged():
    raw = [
        {"limitType": 4, "limitAmount": 500, "attachedPurses": [{"purseId": 7}]},
        {"limitType": 4, "limitAmount": 500, "attachedPurses": [{"purseId": 5}]},
        {"limitType": 3, "limitAmount": 500, "attachedPurses": [{"purseId": 7}]},
    ]
    limits = lim.ukrnafta_limits(raw, FUEL_PURSES)
    assert lim.short(limits) == "Д500 М500"
    assert limits[0]["fuels"] == ["А-95", "ДП"]


def test_petroline_washer_and_water_are_not_fuel():
    assert lim.petroline_limits(PL) == []


def test_petroline_fuel_limit_once_and_day():
    limits = lim.petroline_limits([pl_fuel_record(), pl_fuel_record(limitStatus=3, onceLimit=999)])
    assert lim.short(limits) == "Р20 Д120"
    assert lim.describe(limits)[1] == "Д120 — на добу, літри: ДП"


def test_petroline_by_person():
    by_person = lim.petroline_by_person([pl_fuel_record(personId=7), *PL])
    assert lim.short(by_person["7"]) == "Р20 Д120"
    assert by_person[str(PL[0]["personId"])] == []


def test_help_text_explains_codes():
    for code in ("Р", "Д", "Т", "М", "С", "₴"):
        assert code in lim.HELP_TEXT


# --- адаптер UkrNafta ---------------------------------------------------------------------------


class FakeResponse:
    ok = True
    status_code = 200

    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


def test_adapter_reads_limits_and_fuel_purses(monkeypatch):
    monkeypatch.setenv("UKRNAFTA_TOKEN", "t")
    urls = []

    def fake_get(url, **kwargs):
        urls.append(url)
        return FakeResponse(UN["purses"] if url.endswith("/common/purses") else UN["card_limits"][CARD_TWO])

    monkeypatch.setattr(ukrnafta_cards.requests, "get", fake_get)
    client = UkrnaftaCards()
    assert len(client.list_limits(CARD_TWO)) == 2
    purses = client.fuel_purses()
    assert purses[7] == "ДП" and 20 not in purses  # «Мийка» — не пальне
    assert urls[0].endswith(f"/cards/{CARD_TWO}/limits")


def test_adapter_error_in_body(monkeypatch):
    monkeypatch.setenv("UKRNAFTA_TOKEN", "t")
    monkeypatch.setattr(
        ukrnafta_cards.requests,
        "get",
        lambda url, **kw: FakeResponse({"errorCode": 5, "errorDescription": "x"}),
    )
    with pytest.raises(UkrnaftaError):
        UkrnaftaCards().list_limits(CARD_ONE)


def test_network_is_blocked_in_tests():
    with pytest.raises(NetworkBlocked):
        requests.get("https://naftacards.ukrnafta.com/public-api/common/ping", timeout=1)


# --- знімок при оновленні карток ----------------------------------------------------------------


class FakeUkrnafta:
    """Підміна UkrnaftaCards для синхронізації і розблокування."""

    cards: list[dict] = []
    limit_calls: list[str] = []
    fail_cards: set[str] = set()
    purses_fail = False

    def list_cards(self):
        return FakeUkrnafta.cards

    def fuel_purses(self):
        if FakeUkrnafta.purses_fail:
            raise UkrnaftaError("HTTP 500")
        return FUEL_PURSES

    def list_limits(self, card_id):
        FakeUkrnafta.limit_calls.append(card_id)
        if card_id in FakeUkrnafta.fail_cards:
            raise UkrnaftaError("HTTP 500")
        return un_limits(card_id)

    def update_card(self, card_number, **kwargs):
        pass


@pytest.fixture
def fake_ukrnafta(monkeypatch, db):
    ProviderConfig.ensure_all()
    FakeUkrnafta.cards = [
        {"cardId": CARD_ONE, "status": 0},
        {"cardId": CARD_TWO, "status": 0},
        {"cardId": CARD_EMPTY, "status": 1},
    ]
    FakeUkrnafta.limit_calls = []
    FakeUkrnafta.fail_cards = set()
    FakeUkrnafta.purses_fail = False
    monkeypatch.setattr(card_service, "UkrnaftaCards", FakeUkrnafta)
    monkeypatch.setitem(card_service.CLIENTS, "ukrnafta", FakeUkrnafta)
    return FakeUkrnafta


OLD = [{"period": "Д", "money": False, "amount": 1.0, "fuels": ["ДП"]}]


def sync_ukrnafta_all() -> None:
    card_service.sync_ukrnafta_cards()
    card_service.sync_ukrnafta_limits()


def test_card_sync_does_not_read_limits(fake_ukrnafta):
    card_service.sync_ukrnafta_cards()
    assert fake_ukrnafta.limit_calls == []
    assert Card.objects.filter(provider="ukrnafta").count() == 3


def test_limits_sync_reads_active_cards_only(fake_ukrnafta):
    card_service.sync_ukrnafta_cards()
    Card.objects.filter(number=CARD_EMPTY).update(fuel_limits=OLD)
    assert card_service.sync_ukrnafta_limits() == (2, 0)
    assert fake_ukrnafta.limit_calls == [CARD_ONE, CARD_TWO]
    assert Card.objects.get(number=CARD_TWO).limits_short == "Д60 М60"
    blocked = Card.objects.get(number=CARD_EMPTY)
    assert blocked.fuel_limits == OLD and blocked.limits_updated_at is None


def test_limits_sync_card_error_keeps_old_snapshot(fake_ukrnafta):
    card_service.sync_ukrnafta_cards()
    Card.objects.filter(number=CARD_ONE).update(fuel_limits=OLD)
    fake_ukrnafta.fail_cards = {CARD_ONE}
    assert card_service.sync_ukrnafta_limits() == (1, 1)
    assert Card.objects.get(number=CARD_ONE).fuel_limits == OLD
    assert Card.objects.get(number=CARD_TWO).limits_short == "Д60 М60"


def test_limits_sync_purses_error_changes_nothing_and_is_audited(fake_ukrnafta):
    card_service.sync_ukrnafta_cards()
    fake_ukrnafta.purses_fail = True
    with pytest.raises(UkrnaftaError):
        card_service.sync_limits("ukrnafta")
    assert fake_ukrnafta.limit_calls == []
    entry = AuditLog.objects.get(action=AuditLog.Action.LIMITS_SYNC)
    assert not entry.success and "HTTP 500" in entry.error


def test_limits_sync_is_audited_and_idempotent(fake_ukrnafta):
    card_service.sync_ukrnafta_cards()
    card_service.sync_limits("ukrnafta")
    fake_ukrnafta.fail_cards = {CARD_ONE}
    card_service.sync_limits("ukrnafta")
    assert Card.objects.get(number=CARD_TWO).limits_short == "Д60 М60"
    last = AuditLog.objects.filter(action=AuditLog.Action.LIMITS_SYNC).order_by("-pk").first()
    assert last.details["updated"] == 1 and "З помилкою: 1" in last.details_display()
    # частковий збій видно у фільтрі «Лише помилки»
    assert not last.success and "1 карток" in last.error
    first = AuditLog.objects.filter(action=AuditLog.Action.LIMITS_SYNC).order_by("pk").first()
    assert first.success and first.error == ""


def test_limits_sync_not_supported_for_petroline(db):
    ProviderConfig.ensure_all()
    with pytest.raises(ValueError):
        card_service.sync_limits("petroline")


def petroline_row(number: str, person: str, card_type: int = 1) -> dict:
    return {
        "number": number,
        "ext_number": "",
        "external_id": number,
        "api_status": 3,
        "status": CardStatus.ACTIVE,
        "card_type": card_type,
        "person_id": person,
        "transport_id": "" if person else "77",
        "label": "X",
        "vehicle": "X",
        "driver_name": "",
    }


def test_sync_petroline_sets_person_limits(monkeypatch, db):
    ProviderConfig.ensure_all()
    rows = [petroline_row("ID-1", "7"), petroline_row("ID-2", "8"), petroline_row("ID-3", "", card_type=5)]
    monkeypatch.setattr(card_service.PetrolineCards, "list_cards", lambda self: rows)
    monkeypatch.setattr(
        card_service.PetrolineCards, "person_limits", lambda self: [pl_fuel_record(personId=7), *PL]
    )
    card_service.sync_petroline_cards()
    assert Card.objects.get(number="ID-1").limits_short == "Р20 Д120"
    assert Card.objects.get(number="ID-2").fuel_limits == []
    assert Card.objects.get(number="ID-3").limits_updated_at is not None


def test_sync_petroline_limits_error_keeps_old_snapshot(monkeypatch, db):
    ProviderConfig.ensure_all()
    Card.objects.create(provider="petroline", number="ID-1", fuel_limits=OLD)

    def fail(self):
        raise RuntimeError("Petroline недоступний")

    monkeypatch.setattr(card_service.PetrolineCards, "list_cards", lambda self: [petroline_row("ID-1", "7")])
    monkeypatch.setattr(card_service.PetrolineCards, "person_limits", fail)
    created, updated, warning = card_service.sync_petroline_cards()
    card = Card.objects.get(number="ID-1")
    assert card.fuel_limits == OLD and card.provider_person_id == "7"
    assert updated == 1 and "ліміти не оновлено" in warning and "Petroline недоступний" in warning


def test_card_sync_with_limits_warning_is_audited_as_error(monkeypatch, logged_client):
    ProviderConfig.ensure_all()
    ProviderConfig.objects.filter(code="petroline").update(is_active=True)
    monkeypatch.setitem(card_service.SYNCERS, "petroline", lambda: (0, 5, "ліміти не оновлено: HTTP 500"))
    resp = logged_client.post(
        reverse("cards_sync"), json.dumps({"provider": "petroline"}), content_type="application/json"
    )
    assert resp.json() == {
        "created": 0,
        "updated": 5,
        "failed": True,
        "message": "нових 0, оновлено 5; ліміти не оновлено: HTTP 500",
    }
    entry = AuditLog.objects.get(action=AuditLog.Action.CARDS_SYNC)
    assert not entry.success and entry.details["updated"] == 5


def test_empty_petroline_limits_clear_snapshot(monkeypatch, db):
    """Порожній список від API — «лімітів немає» (знятий ліміт зникає), а не збій."""
    ProviderConfig.ensure_all()
    Card.objects.create(provider="petroline", number="ID-1", fuel_limits=OLD)
    monkeypatch.setattr(card_service.PetrolineCards, "list_cards", lambda self: [petroline_row("ID-1", "7")])
    monkeypatch.setattr(card_service.PetrolineCards, "person_limits", lambda self: [])
    assert card_service.sync_petroline_cards()[2] == ""
    assert Card.objects.get(number="ID-1").fuel_limits == []


# --- розблокування ------------------------------------------------------------------------------


def test_unblock_refreshes_limits_block_does_not(fake_ukrnafta):
    card = Card.objects.create(provider="ukrnafta", number=CARD_TWO, status=CardStatus.BLOCKED)
    card_service.set_status(card, CardStatus.ACTIVE)
    card.refresh_from_db()
    assert card.limits_short == "Д60 М60"
    fake_ukrnafta.limit_calls = []
    card_service.set_status(card, CardStatus.BLOCKED)
    assert fake_ukrnafta.limit_calls == []


def test_unblock_survives_limits_error(fake_ukrnafta):
    card = Card.objects.create(
        provider="ukrnafta", number=CARD_ONE, status=CardStatus.BLOCKED, fuel_limits=OLD
    )
    fake_ukrnafta.fail_cards = {CARD_ONE}
    card_service.set_status(card, CardStatus.ACTIVE)
    card.refresh_from_db()
    assert card.status == CardStatus.ACTIVE and card.fuel_limits == OLD


def test_unblock_petroline_refreshes_person_limits(monkeypatch, db):
    ProviderConfig.ensure_all()
    ProviderConfig.objects.filter(code="petroline").update(edit_enabled=True, is_active=True)

    class FakePetroline:
        def update_card(self, number, *, status):
            pass

        def person_limits(self):
            return [pl_fuel_record(personId=7)]

    monkeypatch.setattr(card_service, "PetrolineCards", FakePetroline)
    monkeypatch.setitem(card_service.CLIENTS, "petroline", FakePetroline)
    card = Card.objects.create(
        provider="petroline",
        number="ID-1",
        status=CardStatus.BLOCKED,
        provider_card_type=1,
        provider_person_id="7",
    )
    card_service.set_status(card, CardStatus.ACTIVE)
    card.refresh_from_db()
    assert card.limits_short == "Р20 Д120"


# --- відображення -------------------------------------------------------------------------------


@pytest.fixture
def logged_client(client, django_user_model):
    client.force_login(django_user_model.objects.create_user(username="manager", password="x"))
    return client


def test_cards_page_shows_limits_column_with_help(logged_client, fake_ukrnafta):
    sync_ukrnafta_all()
    html = logged_client.get(reverse("cards")).content.decode()
    assert "Ліміти" in html and "Д60 М60" in html
    assert "Д — на добу" in html  # підказка


def test_card_page_shows_limits_details_and_note(logged_client, fake_ukrnafta):
    sync_ukrnafta_all()
    card = Card.objects.get(number=CARD_TWO)
    html = logged_client.get(reverse("card_detail", args=[card.pk])).content.decode()
    assert "Д60 — на добу, літри: ДП, ДП ENERGY" in html
    blocked = Card.objects.get(number=CARD_EMPTY)
    html = logged_client.get(reverse("card_detail", args=[blocked.pk])).content.decode()
    assert "ще не читались" in html and "після розблокування ліміти оновляться" in html


def test_cards_list_unblock_returns_limits(logged_client, fake_ukrnafta):
    card = Card.objects.create(provider="ukrnafta", number=CARD_TWO, status=CardStatus.BLOCKED)
    resp = logged_client.post(
        reverse("card_status", args=[card.pk]), json.dumps({"status": 0}), content_type="application/json"
    )
    assert resp.json()["card"]["limits_short"] == "Д60 М60"


def test_cards_page_has_separate_limits_button(logged_client, fake_ukrnafta):
    html = logged_client.get(reverse("cards")).content.decode()
    assert "Оновити статуси UkrNafta" in html and "Оновити ліміти UkrNafta" in html


def test_cards_sync_endpoint_limits_kind(logged_client, fake_ukrnafta):
    card_service.sync_ukrnafta_cards()
    resp = logged_client.post(
        reverse("cards_sync"),
        json.dumps({"provider": "ukrnafta", "kind": "limits"}),
        content_type="application/json",
    )
    assert resp.json() == {"updated": 2, "failed": 0, "message": "оновлено 2"}


def test_card_page_refresh_limits_button(logged_client, fake_ukrnafta):
    card = Card.objects.create(provider="ukrnafta", number=CARD_TWO, status=CardStatus.ACTIVE)
    url = reverse("card_detail", args=[card.pk])
    assert "Оновити ліміти" in logged_client.get(url).content.decode()
    html = logged_client.post(url, {"action": "limits"}, follow=True).content.decode()
    assert "Ліміти оновлено: Д60 М60" in html
    assert fake_ukrnafta.limit_calls == [CARD_TWO]


def test_card_page_refresh_limits_error_keeps_snapshot(logged_client, fake_ukrnafta):
    card = Card.objects.create(
        provider="ukrnafta", number=CARD_ONE, status=CardStatus.ACTIVE, fuel_limits=OLD
    )
    fake_ukrnafta.fail_cards = {CARD_ONE}
    html = logged_client.post(reverse("card_detail", args=[card.pk]), {"action": "limits"}, follow=True)
    assert "Помилка провайдера" in html.content.decode()
    card.refresh_from_db()
    assert card.fuel_limits == OLD


def test_upg_card_has_no_refresh_limits_button(logged_client, db):
    ProviderConfig.ensure_all()
    card = Card.objects.create(provider="upg", number="000 001")
    html = logged_client.get(reverse("card_detail", args=[card.pk])).content.decode()
    assert "Оновити ліміти" not in html and "UPG не надає лімітів" in html


def test_card_page_unexpected_error_shows_message(logged_client, fake_ukrnafta, monkeypatch):
    card = Card.objects.create(
        provider="ukrnafta", number=CARD_ONE, status=CardStatus.ACTIVE, fuel_limits=OLD
    )

    def broken(self, card_id):
        raise KeyError("limitType")

    monkeypatch.setattr(FakeUkrnafta, "list_limits", broken)
    resp = logged_client.post(reverse("card_detail", args=[card.pk]), {"action": "limits"}, follow=True)
    assert resp.status_code == 200
    assert "Несподівана помилка" in resp.content.decode()
    card.refresh_from_db()
    assert card.fuel_limits == OLD
