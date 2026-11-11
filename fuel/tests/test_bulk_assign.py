"""Масова зміна типу авто або менеджера відмічених на «Картах» карток.

Лише в нашій базі — API провайдерів не викликається (клієнти підмінено, мережа заблокована).
"""

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from fuel import card_service
from fuel.managers import MANAGERS_GROUP
from fuel.models import AuditLog, Card, CardStatus, ProviderConfig, VehicleType


class FailingClient:
    def __init__(self, *args, **kwargs):
        raise AssertionError("Масова зміна не має йти в API провайдера")


@pytest.fixture(autouse=True)
def no_provider_api(monkeypatch):
    monkeypatch.setitem(card_service.CLIENTS, "ukrnafta", FailingClient)
    monkeypatch.setitem(card_service.CLIENTS, "petroline", FailingClient)
    monkeypatch.setattr(card_service, "PetrolineCards", FailingClient)


@pytest.fixture
def make_user(django_user_model):
    def make(username: str, *, manager: bool = True, **fields):
        user = django_user_model.objects.create_user(username=username, password="x", **fields)
        if manager:
            user.groups.add(Group.objects.get(name=MANAGERS_GROUP))
        return user

    return make


@pytest.fixture
def me(make_user):
    return make_user("me", first_name="Олена", last_name="Коваль")


@pytest.fixture
def logged_client(client, me):
    client.force_login(me)
    return client


@pytest.fixture
def mixer(db):
    return VehicleType.objects.create(name="Міксер")


def make_card(number: str, provider: str = "ukrnafta", **fields) -> Card:
    ProviderConfig.ensure_all()
    return Card.objects.create(provider=provider, number=number, status=CardStatus.ACTIVE, **fields)


def bulk(client, ids, field, value):
    return client.post(
        reverse("cards_bulk"), {"ids": ids, "field": field, "value": value}, content_type="application/json"
    )


@pytest.mark.django_db
def test_bulk_vehicle_type_changes_only_needed_cards_with_audit(logged_client, mixer):
    a, b = make_card("1"), make_card("2", provider="upg")
    already = make_card("3", vehicle_type=mixer)
    untouched = make_card("4")

    response = bulk(logged_client, [a.pk, b.pk, already.pk], "vehicle_type", mixer.pk)

    assert response.status_code == 200
    data = response.json()
    assert (data["changed"], data["unchanged"], data["missing"], data["label"]) == (2, 1, 0, "Міксер")
    assert {c["id"]: c["vehicle_type_id"] for c in data["cards"]} == {
        a.pk: mixer.pk,
        b.pk: mixer.pk,
        already.pk: mixer.pk,
    }
    assert set(Card.objects.filter(vehicle_type=mixer).values_list("pk", flat=True)) == {
        a.pk,
        b.pk,
        already.pk,
    }
    untouched.refresh_from_db()
    assert untouched.vehicle_type is None
    # Аудит — лише для справді змінених карток, по запису на картку
    entries = AuditLog.objects.order_by("card_id")
    assert [e.card_id for e in entries] == [a.pk, b.pk]
    assert all(e.changes == {"vehicle_type": [None, "Міксер"]} and e.username == "me" for e in entries)


@pytest.mark.django_db
def test_bulk_clears_vehicle_type(logged_client, mixer):
    card = make_card("1", vehicle_type=mixer)

    data = bulk(logged_client, [card.pk], "vehicle_type", "").json()

    assert (data["changed"], data["label"]) == (1, "")
    card.refresh_from_db()
    assert card.vehicle_type is None
    assert AuditLog.objects.get().changes == {"vehicle_type": ["Міксер", None]}


@pytest.mark.django_db
def test_bulk_manager_assign_and_clear(logged_client, make_user):
    other = make_user("other", first_name="Петро", last_name="Бондар")
    a, b = make_card("1"), make_card("2")

    data = bulk(logged_client, [a.pk, b.pk], "manager", other.pk).json()

    assert (data["changed"], data["label"]) == (2, "Петро Бондар")
    assert Card.objects.filter(manager=other).count() == 2
    assert AuditLog.objects.filter(changes__manager=[None, "Петро Бондар"]).count() == 2

    data = bulk(logged_client, [a.pk, b.pk], "manager", "").json()

    assert data["changed"] == 2
    assert Card.objects.filter(manager__isnull=True).count() == 2


@pytest.mark.django_db
def test_bulk_rejects_non_manager_without_changes(logged_client, make_user):
    outsider = make_user("outsider", manager=False)
    inactive = make_user("gone", is_active=False)
    card = make_card("1")

    for value in (outsider.pk, inactive.pk, "abc", 999999):
        response = bulk(logged_client, [card.pk], "manager", value)
        assert response.status_code == 400, value

    card.refresh_from_db()
    assert card.manager is None
    assert not AuditLog.objects.exists()


@pytest.mark.django_db
def test_bulk_former_manager_can_only_be_removed(logged_client, make_user):
    """Менеджера, якого прибрали з групи, масово не призначити, але зняти можна."""
    former = make_user("former")
    card = make_card("1", manager=former)
    fresh = make_card("2")
    former.groups.clear()

    assert bulk(logged_client, [fresh.pk], "manager", former.pk).status_code == 400
    assert bulk(logged_client, [card.pk], "manager", "").json()["changed"] == 1
    card.refresh_from_db()
    assert card.manager is None


@pytest.mark.django_db
def test_bulk_rejects_unknown_vehicle_type(logged_client):
    card = make_card("1")

    response = bulk(logged_client, [card.pk], "vehicle_type", 999999)

    assert response.status_code == 400
    assert response.json()["error"] == "Невідомий тип авто"
    assert not AuditLog.objects.exists()


@pytest.mark.django_db
def test_bulk_counts_missing_cards(logged_client, mixer):
    card = make_card("1")

    data = bulk(logged_client, [card.pk, card.pk, 999999], "vehicle_type", mixer.pk).json()

    assert (data["changed"], data["unchanged"], data["missing"]) == (1, 0, 1)


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("ids", "field"),
    [
        ([], "vehicle_type"),
        (None, "vehicle_type"),
        ("1,2", "vehicle_type"),
        (["1"], "vehicle_type"),
        ([True], "vehicle_type"),
        ([1], "vehicle"),
        ([1], None),
    ],
)
def test_bulk_rejects_bad_request(logged_client, mixer, ids, field):
    make_card("1")

    response = bulk(logged_client, ids, field, mixer.pk)

    assert response.status_code == 400
    assert not Card.objects.filter(vehicle_type=mixer).exists()


@pytest.mark.django_db
def test_bulk_limits_number_of_cards(logged_client, mixer, monkeypatch):
    monkeypatch.setattr(card_service, "BULK_MAX_CARDS", 2)
    cards = [make_card(str(n)) for n in range(3)]

    response = bulk(logged_client, [c.pk for c in cards], "vehicle_type", mixer.pk)

    assert response.status_code == 400
    assert not Card.objects.filter(vehicle_type=mixer).exists()


@pytest.mark.django_db
def test_bulk_requires_login_and_post(client, me, mixer):
    card = make_card("1")

    assert bulk(client, [card.pk], "vehicle_type", mixer.pk).status_code == 302

    client.force_login(me)
    assert client.get(reverse("cards_bulk")).status_code == 405
    card.refresh_from_db()
    assert card.vehicle_type is None


@pytest.mark.django_db
def test_cards_page_has_bulk_panel(logged_client, mixer):
    make_card("1")

    html = logged_client.get(reverse("cards")).content.decode()

    assert 'id="bulk"' in html
    assert 'class="sel-box"' in html
    assert 'id="bulk-vtype"' in html and "Міксер" in html
    assert 'id="bulk-mgr"' in html and "Олена Коваль" in html
