"""Відповідальний менеджер картки: користувач із групи «Менеджери».

Призначення, відображення, фільтри на «Транзакціях», «Картах» і «Логах». Лише в нашій базі —
API провайдерів не викликається (клієнти підмінено, мережа заблокована).
"""

from datetime import datetime

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse
from django.utils import timezone

from fuel import card_service
from fuel.managers import MANAGERS_GROUP, user_label
from fuel.models import AuditLog, Card, CardStatus, ProviderConfig, Transaction


class FailingClient:
    def __init__(self, *args, **kwargs):
        raise AssertionError("Менеджер не має йти в API провайдера")


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
def other(make_user):
    return make_user("other", first_name="Петро", last_name="Бондар", email="petro@example.com")


def make_card(number: str, provider: str = "ukrnafta", **fields) -> Card:
    ProviderConfig.ensure_all()
    return Card.objects.create(provider=provider, number=number, status=CardStatus.ACTIVE, **fields)


def make_tx(card: Card | None, external_id: str) -> Transaction:
    ts = timezone.make_aware(datetime(2026, 11, 3, 12, 0))
    return Transaction.objects.create(
        provider=card.provider if card else "upg",
        external_id=external_id,
        card=card,
        card_ref=card.number if card else "X",
        timestamp=ts,
    )


# --- група і підписи ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_migration_creates_managers_group():
    assert Group.objects.filter(name=MANAGERS_GROUP).exists()


@pytest.mark.django_db
def test_user_label_prefers_full_name(make_user):
    assert user_label(make_user("a", first_name="Олена", last_name="Коваль")) == "Олена Коваль"
    assert user_label(make_user("login_only")) == "login_only"


# --- призначення --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_cards_list_sets_manager_locally_with_audit(logged_client, other):
    card = make_card("1")
    url = reverse("card_manager", args=[card.pk])

    response = logged_client.post(url, {"manager": other.pk}, content_type="application/json")

    assert response.status_code == 200
    assert response.json()["card"]["manager"] == "Петро Бондар"
    card.refresh_from_db()
    assert card.manager == other
    entry = AuditLog.objects.get()
    assert (entry.action, entry.via_api, entry.username) == (AuditLog.Action.CARD_INFO, False, "me")
    assert entry.changes_display() == ["Менеджер: «» → «Петро Бондар»"]

    logged_client.post(url, {"manager": other.pk}, content_type="application/json")
    assert AuditLog.objects.count() == 1
    logged_client.post(url, {"manager": ""}, content_type="application/json")
    card.refresh_from_db()
    assert card.manager is None


@pytest.mark.django_db
def test_only_active_group_members_can_be_assigned(logged_client, make_user):
    card = make_card("1")
    url = reverse("card_manager", args=[card.pk])
    outsider = make_user("outsider", manager=False)
    inactive = make_user("inactive", is_active=False)

    for user in (outsider, inactive):
        response = logged_client.post(url, {"manager": user.pk}, content_type="application/json")
        assert response.status_code == 400
    response = logged_client.post(url, {"manager": "abc"}, content_type="application/json")
    assert response.status_code == 400
    assert AuditLog.objects.count() == 0


@pytest.mark.django_db
def test_former_manager_stays_on_card_detail_save(logged_client, make_user):
    """Менеджера прибрали з групи: збереження форми картки його не знімає й не падає."""
    former = make_user("former", first_name="Іван", last_name="Старий")
    card = make_card("1", vehicle="AA1", manager=former)
    former.groups.clear()

    page = logged_client.get(reverse("card_detail", args=[card.pk])).content.decode()
    assert "Іван Старий (не менеджер)" in page

    payload = {"action": "save", "vehicle": "AA1", "driver_name": "", "note": "Н", "manager": former.pk}
    logged_client.post(reverse("card_detail", args=[card.pk]), payload)

    card.refresh_from_db()
    assert (card.manager, card.note) == (former, "Н")


@pytest.mark.django_db
def test_card_detail_shows_and_saves_manager(logged_client, other):
    card = make_card("1", vehicle="AA1")
    payload = {"action": "save", "vehicle": "AA1", "driver_name": "", "note": "", "manager": other.pk}

    response = logged_client.post(reverse("card_detail", args=[card.pk]), payload, follow=True)

    html = response.content.decode()
    assert "Зміни збережено" in html
    assert "petro@example.com" in html
    card.refresh_from_db()
    assert card.manager == other


@pytest.mark.django_db
def test_create_card_with_manager(logged_client, other):
    ProviderConfig.ensure_all()
    data = {"provider": "upg", "number": "777", "manager": other.pk}

    response = logged_client.post(reverse("card_create"), data, content_type="application/json")

    assert response.status_code == 200
    assert Card.objects.get(number="777").manager == other
    assert AuditLog.objects.get().changes["manager"] == [None, "Петро Бондар"]


@pytest.mark.django_db
def test_deleting_user_clears_cards_and_audits(other):
    card = make_card("1", manager=other)

    other.delete()

    card.refresh_from_db()
    assert card.manager is None
    assert AuditLog.objects.get(card=card).changes == {"manager": ["Петро Бондар", None]}


@pytest.mark.django_db
def test_cards_page_has_manager_select_and_filter(logged_client, other):
    make_card("1", manager=other)

    html = logged_client.get(reverse("cards")).content.decode()

    assert 'id="f-mgr"' in html
    assert f'data-mgr="{other.pk}"' in html
    assert f'<option value="{other.pk}" selected>Петро Бондар</option>' in html


# --- фільтри ------------------------------------------------------------------------------------


@pytest.fixture
def tx_set(me, other):
    return {
        "mine": make_tx(make_card("1", manager=me), "t1"),
        "other": make_tx(make_card("2", manager=other), "t2"),
        "nobody": make_tx(make_card("3"), "t3"),
        "no_card": make_tx(None, "t4"),
    }


def tx_ids(client, **params) -> set[str]:
    response = client.get(reverse("transactions"), {"from": "2026-11-03", "to": "2026-11-03", **params})
    return {t.external_id for t in response.context["page"]}


@pytest.mark.django_db
def test_transactions_filter_by_manager(logged_client, tx_set, other):
    assert tx_ids(logged_client) == {"t1", "t2", "t3", "t4"}
    assert tx_ids(logged_client, mgr="me") == {"t1"}
    assert tx_ids(logged_client, mgr=other.pk) == {"t2"}
    assert tx_ids(logged_client, mgr="none") == {"t3", "t4"}
    assert tx_ids(logged_client, mgr="999") == {"t1", "t2", "t3", "t4"}


@pytest.mark.django_db
def test_transactions_show_manager_column_and_csv(logged_client, tx_set):
    params = {"from": "2026-11-03", "to": "2026-11-03", "mgr": "me"}
    response = logged_client.get(reverse("transactions"), params)
    assert '<td class="c-manager own">Олена Коваль</td>' in response.content.decode()
    assert "mgr=me" in response.context["query"]

    csv = logged_client.get(reverse("transactions"), {**params, "mgr": "", "export": "csv"})
    lines = csv.content.decode("utf-8-sig").splitlines()
    column = lines[0].split(";").index("Менеджер")
    assert {line.split(";")[column] for line in lines[1:]} == {"Олена Коваль", "Петро Бондар", ""}


@pytest.mark.django_db
def test_logs_filter_by_manager(logged_client, me, other):
    card_service.record_audit(AuditLog.Action.CARD_INFO, make_card("1", manager=me))
    card_service.record_audit(AuditLog.Action.CARD_INFO, make_card("2", manager=other))
    AuditLog.objects.create(action=AuditLog.Action.TX_SYNC, provider="upg")

    def numbers(**params):
        response = logged_client.get(reverse("logs"), params)
        return sorted(e.card.number if e.card else "sync" for e in response.context["page"])

    assert numbers() == ["1", "2", "sync"]
    assert numbers(mgr="me") == ["1"]
    assert numbers(mgr=other.pk) == ["2"]
