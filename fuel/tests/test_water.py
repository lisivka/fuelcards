"""Картки на воду (ЗАДАЧА-16): правило «вода в назві», позначка при «Оновити карти Petroline»,
ручна зміна менеджером (рядок «Карт», сторінка картки), фільтр на «Картах», міграція 0013.

API провайдерів не викликається: список карток Petroline підмінено, мережа заблокована (conftest.py).
"""

import importlib
import json

import pytest
from django.apps import apps
from django.urls import reverse

from fuel import card_service
from fuel.models import AuditLog, Card, CardStatus, ProviderConfig
from fuel.water import has_water_word


@pytest.mark.parametrize(
    "texts, expected",
    [
        (("Вода SINOTRUK_AI08-62",), True),
        (("ВОДА",), True),
        (("Вода_Man",), True),
        (("Картка для води",), True),
        (("Петренко", "Привезти воду"), True),
        (("Mercedes | Вода",), True),
        (("Водій Петренко",), False),
        (("Водафон", "підводагон"), False),
        (("Водний транспорт",), False),
        (("", None), False),
        ((), False),
    ],
)
def test_has_water_word(texts, expected):
    assert has_water_word(*texts) is expected


def petroline_row(number: str, label: str) -> dict:
    return {
        "number": number,
        "ext_number": "",
        "external_id": number,
        "api_status": 3,
        "status": CardStatus.ACTIVE,
        "card_type": 1,
        "person_id": "7",
        "transport_id": "",
        "label": label,
        "vehicle": label,
        "driver_name": "",
    }


@pytest.fixture
def petroline_api(monkeypatch, db):
    """Підмінений список карток Petroline: rows[:] = [...] перед викликом синхронізації."""
    ProviderConfig.ensure_all()
    rows: list[dict] = []
    monkeypatch.setattr(card_service.PetrolineCards, "list_cards", lambda self: list(rows))
    monkeypatch.setattr(card_service.PetrolineCards, "person_limits", lambda self: [])
    return rows


def test_sync_marks_water_cards_by_name(petroline_api):
    petroline_api[:] = [petroline_row("ID-1", "Вода MAN AA1234"), petroline_row("ID-2", "Петренко Водій")]
    card_service.sync_petroline_cards()
    assert Card.objects.get(number="ID-1").is_water is True
    assert Card.objects.get(number="ID-2").is_water is False


def test_sync_uses_our_vehicle_and_driver_too(petroline_api):
    Card.objects.create(provider="petroline", number="ID-1", vehicle="Бочка вода", driver_name="Іваненко")
    petroline_api[:] = [petroline_row("ID-1", "Іваненко")]
    card_service.sync_petroline_cards()
    assert Card.objects.get(number="ID-1").is_water is True


def test_sync_clears_auto_mark_when_name_changes(petroline_api):
    Card.objects.create(provider="petroline", number="ID-1", is_water=True)
    petroline_api[:] = [petroline_row("ID-1", "MAN AA1234")]
    card_service.sync_petroline_cards()
    assert Card.objects.get(number="ID-1").is_water is False


@pytest.mark.parametrize("manual_value, label", [(False, "Вода MAN"), (True, "MAN AA1234")])
def test_sync_keeps_manual_mark(petroline_api, manual_value, label):
    Card.objects.create(provider="petroline", number="ID-1", is_water=manual_value, is_water_manual=True)
    petroline_api[:] = [petroline_row("ID-1", label)]
    card_service.sync_petroline_cards()
    card = Card.objects.get(number="ID-1")
    assert card.is_water is manual_value and card.is_water_manual


# --- ручна позначка -----------------------------------------------------------------------------


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="manager", password="x")


@pytest.fixture
def logged_client(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def card(db):
    ProviderConfig.ensure_all()
    return Card.objects.create(provider="petroline", number="ID-1", status=CardStatus.ACTIVE)


def test_set_water_is_manual_and_audited(card, user):
    assert card_service.set_water(card, True, user=user) is True
    card.refresh_from_db()
    assert card.is_water and card.is_water_manual
    entry = AuditLog.objects.get(action=AuditLog.Action.CARD_INFO, card=card)
    assert entry.changes == {"is_water": [False, True]}
    assert entry.changes_display() == ["Картка на воду: «ні» → «так»"]
    assert entry.username == "manager" and not entry.via_api


def test_set_water_without_change_does_nothing(card):
    assert card_service.set_water(card, False) is False
    card.refresh_from_db()
    assert not card.is_water_manual
    assert not AuditLog.objects.exists()


def test_water_endpoint_toggles(logged_client, card):
    url = reverse("card_water", args=[card.pk])
    resp = logged_client.post(url, json.dumps({"is_water": True}), content_type="application/json")
    assert resp.status_code == 200
    data = resp.json()
    assert data["changed"] is True and data["card"]["is_water"] is True and data["card"]["is_water_manual"]
    resp = logged_client.post(url, json.dumps({"is_water": False}), content_type="application/json")
    assert resp.json()["card"]["is_water"] is False
    assert AuditLog.objects.filter(card=card).count() == 2


def test_water_endpoint_rejects_non_bool(logged_client, card):
    url = reverse("card_water", args=[card.pk])
    resp = logged_client.post(url, json.dumps({"is_water": "так"}), content_type="application/json")
    assert resp.status_code == 400
    card.refresh_from_db()
    assert not card.is_water and not card.is_water_manual


def test_water_endpoint_requires_login(client, card):
    resp = client.post(reverse("card_water", args=[card.pk]), "{}", content_type="application/json")
    assert resp.status_code == 302


def test_card_detail_water_action(logged_client, card):
    url = reverse("card_detail", args=[card.pk])
    resp = logged_client.post(url, {"action": "water", "is_water": "1", "back": "cards"}, follow=True)
    card.refresh_from_db()
    assert card.is_water and card.is_water_manual
    assert "позначено як картку на воду" in resp.content.decode()
    assert "Зняти позначку" in resp.content.decode()


# --- список «Карти» -----------------------------------------------------------------------------


def test_cards_page_marks_water_rows(logged_client, card):
    Card.objects.create(provider="petroline", number="ID-2", is_water=True)
    html = logged_client.get(reverse("cards")).content.decode()
    assert html.count('data-water="1"') == 1 and html.count('data-water="0"') == 1
    assert '<option value="fuel" selected>Пальне</option>' in html
    assert '<option value="water">Вода (1)</option>' in html
    assert '<option value="all">Усі карти</option>' in html
    assert '<td class="num">1</td>' in html and '<td class="num">2</td>' in html
    assert 'id="count"' in html


# --- міграція 0013 ------------------------------------------------------------------------------


def test_migration_marks_existing_petroline_water_cards(db):
    migration = importlib.import_module("fuel.migrations.0013_card_is_water")
    ProviderConfig.ensure_all()
    Card.objects.create(provider="petroline", number="P-1", provider_label="Вода MAN")
    Card.objects.create(provider="petroline", number="P-2", driver_name="ВОДА")
    Card.objects.create(provider="petroline", number="P-3", provider_label="Петренко Водій")
    Card.objects.create(provider="ukrnafta", number="U-1", vehicle="Вода")
    migration.mark_water_cards(apps, None)
    assert set(Card.objects.filter(is_water=True).values_list("number", flat=True)) == {"P-1", "P-2"}
