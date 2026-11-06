"""Тип авто: довідник (адмінка), призначення картці, фільтри на «Транзакціях», «Картах» і «Логах».

Тип авто живе лише в нашій базі — API провайдерів не викликається (клієнти підмінено, мережа заблокована).
"""

from datetime import datetime

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from fuel import card_service
from fuel.models import AuditLog, Card, CardStatus, ProviderConfig, Transaction, VehicleType


class FailingClient:
    """Будь-який виклик API провайдера — помилка тесту."""

    def __init__(self, *args, **kwargs):
        raise AssertionError("Тип авто не має йти в API провайдера")


@pytest.fixture(autouse=True)
def no_provider_api(monkeypatch):
    monkeypatch.setitem(card_service.CLIENTS, "ukrnafta", FailingClient)
    monkeypatch.setitem(card_service.CLIENTS, "petroline", FailingClient)
    monkeypatch.setattr(card_service, "PetrolineCards", FailingClient)


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="manager", password="x")


@pytest.fixture
def logged_client(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def admin_client_(client, django_user_model):
    admin = django_user_model.objects.create_superuser(username="boss", password="x")
    client.force_login(admin)
    return client


@pytest.fixture
def types(db):
    return VehicleType.objects.create(name="Міксер"), VehicleType.objects.create(name="Тягач")


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


# --- модель і довідник --------------------------------------------------------------------------


@pytest.mark.django_db
def test_duplicate_name_ignores_case_and_spaces(types):
    duplicate = VehicleType(name="  міксер ")
    with pytest.raises(ValidationError):
        duplicate.full_clean()


@pytest.mark.django_db
def test_delete_type_clears_cards_and_audits(admin_client_, types):
    mixer, _ = types
    card = make_card("1", vehicle_type=mixer)

    url = reverse("admin:fuel_vehicletype_delete", args=[mixer.pk])
    response = admin_client_.post(url, {"post": "yes"})

    assert response.status_code == 302
    card.refresh_from_db()
    assert card.vehicle_type is None
    entry = AuditLog.objects.get(card=card)
    assert entry.changes == {"vehicle_type": ["Міксер", None]}
    assert entry.username == "boss"


@pytest.mark.django_db
def test_regular_user_cannot_manage_types(logged_client):
    response = logged_client.get(reverse("admin:fuel_vehicletype_changelist"))
    assert response.status_code == 302  # на вхід в адмінку


# --- призначення картці -------------------------------------------------------------------------


@pytest.mark.django_db
def test_cards_list_sets_type_locally_with_audit(logged_client, types):
    mixer, _ = types
    card = make_card("1")
    url = reverse("card_vehicle_type", args=[card.pk])

    response = logged_client.post(url, {"vehicle_type": mixer.pk}, content_type="application/json")

    assert response.status_code == 200
    assert response.json()["card"]["vehicle_type"] == "Міксер"
    card.refresh_from_db()
    assert card.vehicle_type == mixer
    entry = AuditLog.objects.get()
    assert (entry.action, entry.via_api) == (AuditLog.Action.CARD_INFO, False)
    assert entry.changes_display() == ["Тип авто: «» → «Міксер»"]

    # Той самий тип повторно — без запису в журнал; порожнє значення знімає тип
    logged_client.post(url, {"vehicle_type": mixer.pk}, content_type="application/json")
    assert AuditLog.objects.count() == 1
    logged_client.post(url, {"vehicle_type": ""}, content_type="application/json")
    card.refresh_from_db()
    assert card.vehicle_type is None


@pytest.mark.django_db
def test_cards_list_rejects_unknown_type(logged_client):
    card = make_card("1")
    url = reverse("card_vehicle_type", args=[card.pk])

    response = logged_client.post(url, {"vehicle_type": 999}, content_type="application/json")

    assert response.status_code == 400
    assert AuditLog.objects.count() == 0


@pytest.mark.django_db
def test_card_detail_saves_type_without_api(logged_client, types):
    _, tractor = types
    card = make_card("1", vehicle="AA1111AA", driver_name="Іваненко Іван")
    payload = {
        "action": "save",
        "vehicle": "AA1111AA",
        "driver_name": "Іваненко Іван",
        "note": "",
        "vehicle_type": tractor.pk,
    }

    response = logged_client.post(reverse("card_detail", args=[card.pk]), payload, follow=True)

    assert "Зміни збережено" in response.content.decode()
    card.refresh_from_db()
    assert card.vehicle_type == tractor


@pytest.mark.django_db
def test_petroline_card_detail_type_change_is_local(logged_client, types):
    """Картка Petroline: зміна лише типу не чіпає Авто/ПІБ і не йде в Petroline."""
    mixer, _ = types
    card = make_card(
        "ID-1",
        provider="petroline",
        vehicle="AA1",
        driver_name="Петренко",
        provider_card_type=1,
        provider_person_id="501",
        provider_label="AA1 | Петренко",
    )
    payload = {
        "action": "save",
        "vehicle": "AA1",
        "driver_name": "Петренко",
        "note": "",
        "vehicle_type": mixer.pk,
    }

    logged_client.post(reverse("card_detail", args=[card.pk]), payload)

    card.refresh_from_db()
    assert (card.vehicle_type, card.vehicle, card.provider_label) == (mixer, "AA1", "AA1 | Петренко")


@pytest.mark.django_db
def test_create_card_with_type(logged_client, types):
    mixer, _ = types
    ProviderConfig.ensure_all()
    data = {"provider": "upg", "number": "777", "vehicle_type": mixer.pk}

    response = logged_client.post(reverse("card_create"), data, content_type="application/json")

    assert response.status_code == 200
    assert Card.objects.get(number="777").vehicle_type == mixer
    assert AuditLog.objects.get().changes["vehicle_type"] == [None, "Міксер"]


@pytest.mark.django_db
def test_cards_page_shows_type_select_and_filter(logged_client, types):
    mixer, _ = types
    make_card("1", vehicle_type=mixer)

    html = logged_client.get(reverse("cards")).content.decode()

    assert 'id="f-vtype"' in html
    assert f'data-vtype="{mixer.pk}"' in html
    assert f'<option value="{mixer.pk}" selected>Міксер</option>' in html


# --- фільтри ------------------------------------------------------------------------------------


@pytest.fixture
def tx_set(types):
    mixer, tractor = types
    return {
        "mixer": make_tx(make_card("1", vehicle_type=mixer), "t1"),
        "tractor": make_tx(make_card("2", vehicle_type=tractor), "t2"),
        "untyped": make_tx(make_card("3"), "t3"),
        "no_card": make_tx(None, "t4"),
    }


def tx_ids(client, **params) -> set[str]:
    response = client.get(reverse("transactions"), {"from": "2026-11-03", "to": "2026-11-03", **params})
    return {t.external_id for t in response.context["page"]}


@pytest.mark.django_db
def test_transactions_filter_by_type(logged_client, tx_set, types):
    mixer, _ = types
    assert tx_ids(logged_client) == {"t1", "t2", "t3", "t4"}
    assert tx_ids(logged_client, vtype=mixer.pk) == {"t1"}
    assert tx_ids(logged_client, vtype="none") == {"t3", "t4"}
    # Невідоме значення ігнорується, як «усі»
    assert tx_ids(logged_client, vtype="999") == {"t1", "t2", "t3", "t4"}
    assert tx_ids(logged_client, vtype="abc") == {"t1", "t2", "t3", "t4"}


@pytest.mark.django_db
def test_transactions_show_type_and_keep_filter_in_query(logged_client, tx_set, types):
    mixer, _ = types
    response = logged_client.get(
        reverse("transactions"), {"from": "2026-11-03", "to": "2026-11-03", "vtype": mixer.pk}
    )
    html = response.content.decode()
    assert '<td class="c-vtype own">Міксер</td>' in html
    assert f"vtype={mixer.pk}" in response.context["query"]


@pytest.mark.django_db
def test_transactions_csv_has_type_column(logged_client, tx_set):
    response = logged_client.get(
        reverse("transactions"), {"from": "2026-11-03", "to": "2026-11-03", "export": "csv"}
    )
    lines = response.content.decode("utf-8-sig").splitlines()
    header = lines[0].split(";")
    column = header.index("Тип авто")
    values = {line.split(";")[column] for line in lines[1:]}
    assert values == {"Міксер", "Тягач", ""}


@pytest.mark.django_db
def test_logs_filter_by_type_shows_only_card_entries(logged_client, types):
    mixer, _ = types
    typed = make_card("1", vehicle_type=mixer)
    untyped = make_card("2")
    card_service.record_audit(AuditLog.Action.CARD_INFO, typed)
    card_service.record_audit(AuditLog.Action.CARD_INFO, untyped)
    AuditLog.objects.create(action=AuditLog.Action.TX_SYNC, provider="upg")

    def labels(**params):
        response = logged_client.get(reverse("logs"), params)
        return sorted(e.card.number if e.card else "sync" for e in response.context["page"])

    assert labels() == ["1", "2", "sync"]
    assert labels(vtype=mixer.pk) == ["1"]
    assert labels(vtype="none") == ["2"]


@pytest.mark.django_db
def test_cards_page_petroline_info_is_readonly(logged_client):
    """Авто/ПІБ Petroline у таблиці «Карти» не редагуються — лише на сторінці картки; UkrNafta — як раніше."""
    petroline = make_card("ID-1", provider="petroline", vehicle="PL-AUTO", provider_card_type=1)
    ukrnafta = make_card("2", vehicle="UN-AUTO")

    html = logged_client.get(reverse("cards")).content.decode()

    pl_row = html[html.index(f'data-id="{petroline.pk}"') :].split("</tr>")[0]
    assert "readonly" in pl_row
    assert "save-btn" not in pl_row
    assert "Змінити на сторінці картки" in pl_row
    un_row = html[html.index(f'data-id="{ukrnafta.pk}"') :].split("</tr>")[0]
    assert "readonly" not in un_row
    assert "save-btn" in un_row
