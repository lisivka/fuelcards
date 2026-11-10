"""Сторінка картки: редагування за провайдером, блокування, журнал аудиту.

API провайдерів підмінено фейковим клієнтом — реальних запитів немає.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from fuel import card_service
from fuel.models import AuditLog, Card, CardStatus, ProviderConfig, Transaction
from fuel.providers.petroline_api import PetrolineError
from fuel.providers.petroline_cards import InfoResult
from fuel.providers.ukrnafta_cards import UkrnaftaError


class FakeClient:
    """Підміна UkrnaftaCards: записує виклики update_card, за потреби падає."""

    calls: list[tuple[str, dict]] = []
    fail_with: Exception | None = None

    def update_card(self, card_number, **kwargs):
        if FakeClient.fail_with is not None:
            raise FakeClient.fail_with
        FakeClient.calls.append((card_number, kwargs))


@pytest.fixture(autouse=True)
def fake_api(monkeypatch):
    FakeClient.calls = []
    FakeClient.fail_with = None
    monkeypatch.setitem(card_service.CLIENTS, "ukrnafta", FakeClient)
    monkeypatch.setitem(card_service.CLIENTS, "petroline", FakeClient)
    return FakeClient


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="manager", password="x")


@pytest.fixture
def logged_client(client, user):
    client.force_login(user)
    return client


@pytest.fixture
def ukrnafta_card(db):
    ProviderConfig.ensure_all()
    return Card.objects.create(
        provider="ukrnafta",
        number="0000111122223333",
        vehicle="AA1111AA",
        driver_name="Іваненко Іван",
        status=CardStatus.ACTIVE,
    )


def detail_url(card: Card) -> str:
    return reverse("card_detail", args=[card.pk])


# --- доступ і відображення ----------------------------------------------------------------------


@pytest.mark.django_db
def test_login_required(client, ukrnafta_card):
    response = client.get(detail_url(ukrnafta_card))

    assert response.status_code == 302
    assert "login" in response.url


@pytest.mark.django_db
def test_unknown_card_is_404(logged_client):
    assert logged_client.get(reverse("card_detail", args=[999])).status_code == 404


@pytest.mark.django_db
def test_ukrnafta_page_offers_api_edit_and_block(logged_client, ukrnafta_card):
    html = logged_client.get(detail_url(ukrnafta_card)).content.decode()

    assert "змінюється у провайдера" in html
    assert ">Заблокувати</button>" in html
    assert "disabled>Заблокувати" not in html


@pytest.mark.django_db
def test_petroline_unknown_status_hint_points_to_petroline(logged_client):
    ProviderConfig.ensure_all()
    card = Card.objects.create(provider="petroline", number="ID-1", status=None)

    caps = logged_client.get(detail_url(card)).context["caps"]

    assert caps["can_toggle_status"] is False
    assert "Оновіть карти Petroline" in caps["status_hint"]


@pytest.mark.django_db
def test_unknown_status_disables_block_with_hint(logged_client, ukrnafta_card):
    ukrnafta_card.status = None
    ukrnafta_card.save()

    caps = logged_client.get(detail_url(ukrnafta_card)).context["caps"]

    assert caps["api_edit"] is True
    assert caps["can_toggle_status"] is False
    assert "Оновіть статуси" in caps["status_hint"]


@pytest.mark.django_db
def test_ukrnafta_with_edit_disabled_warns_local_only(logged_client, ukrnafta_card):
    ProviderConfig.objects.filter(code="ukrnafta").update(edit_enabled=False)

    response = logged_client.get(detail_url(ukrnafta_card))

    assert response.context["caps"]["local_only_warning"] is True
    assert "лише в нашій базі" in response.content.decode()


@pytest.mark.django_db
def test_page_shows_last_transactions(logged_client, ukrnafta_card):
    now = timezone.now()
    for i in range(25):
        Transaction.objects.create(
            provider="ukrnafta", external_id=f"T{i}", card=ukrnafta_card, timestamp=now - timedelta(hours=i)
        )

    ctx = logged_client.get(detail_url(ukrnafta_card)).context

    assert len(ctx["transactions"]) == 20
    assert ctx["transactions"][0].external_id == "T0"
    assert ctx["transactions_total"] == 25


@pytest.mark.django_db
def test_back_target_is_whitelisted(logged_client, ukrnafta_card):
    response = logged_client.get(detail_url(ukrnafta_card), {"back": "https://evil.example"})

    assert response.context["back"] == "transactions"


# --- редагування --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_ukrnafta_edit_goes_to_api_then_db_and_audit(logged_client, ukrnafta_card, fake_api):
    response = logged_client.post(
        detail_url(ukrnafta_card),
        {
            "action": "save",
            "back": "cards",
            "vehicle": "BB2222BB",
            "driver_name": "Петренко  Петро",
            "note": "x",
        },
    )

    assert response.status_code == 302 and response.url.endswith("?back=cards")
    assert fake_api.calls == [
        ("0000111122223333", {"vin": "BB2222BB", "last_name": "Петренко", "first_name": "Петро"})
    ]
    ukrnafta_card.refresh_from_db()
    assert (ukrnafta_card.vehicle, ukrnafta_card.driver_name, ukrnafta_card.note) == (
        "BB2222BB",
        "Петренко Петро",
        "x",
    )
    entry = AuditLog.objects.get()
    assert entry.username == "manager"
    assert entry.action == AuditLog.Action.CARD_INFO
    assert entry.via_api is True and entry.success is True
    assert entry.changes["vehicle"] == ["AA1111AA", "BB2222BB"]
    assert "Авто: «AA1111AA» → «BB2222BB»" in entry.changes_display()


@pytest.mark.django_db
def test_note_only_change_does_not_call_api(logged_client, ukrnafta_card, fake_api):
    logged_client.post(
        detail_url(ukrnafta_card),
        {"action": "save", "vehicle": "AA1111AA", "driver_name": "Іваненко Іван", "note": "нова"},
    )

    assert fake_api.calls == []
    entry = AuditLog.objects.get()
    assert entry.via_api is False
    assert list(entry.changes) == ["note"]


@pytest.mark.django_db
def test_no_changes_writes_nothing(logged_client, ukrnafta_card, fake_api):
    response = logged_client.post(
        detail_url(ukrnafta_card),
        {"action": "save", "vehicle": "AA1111AA", "driver_name": "Іваненко Іван", "note": ""},
        follow=True,
    )

    assert fake_api.calls == []
    assert AuditLog.objects.count() == 0
    assert "Змін немає" in response.content.decode()


@pytest.mark.django_db
def test_api_error_keeps_db_and_audits_failure(logged_client, ukrnafta_card, fake_api):
    fake_api.fail_with = UkrnaftaError("HTTP 500: boom")

    response = logged_client.post(
        detail_url(ukrnafta_card),
        {"action": "save", "vehicle": "BB2222BB", "driver_name": "Іваненко Іван", "note": ""},
        follow=True,
    )

    ukrnafta_card.refresh_from_db()
    assert ukrnafta_card.vehicle == "AA1111AA"
    assert "Помилка провайдера" in response.content.decode()
    entry = AuditLog.objects.get()
    assert entry.success is False and entry.via_api is True
    assert "boom" in entry.error


class FakePetroline:
    """Підміна PetrolineCards у card_service: записує update_info, повертає результат як справжній клієнт."""

    calls: list[tuple[str, dict]] = []
    result: InfoResult | None = None
    fail_with: Exception | None = None

    def update_info(self, card_number, **kwargs):
        if FakePetroline.fail_with is not None:
            raise FakePetroline.fail_with
        FakePetroline.calls.append((card_number, kwargs))
        return FakePetroline.result


@pytest.fixture
def fake_petroline(monkeypatch):
    FakePetroline.calls = []
    FakePetroline.result = None
    FakePetroline.fail_with = None
    monkeypatch.setattr(card_service, "PetrolineCards", FakePetroline)
    return FakePetroline


def petroline_card(number="ID-1", card_type=1, person="501", transport="", **fields) -> Card:
    ProviderConfig.ensure_all()
    return Card.objects.create(
        provider="petroline",
        number=number,
        status=fields.pop("status", CardStatus.ACTIVE),
        provider_card_type=card_type,
        provider_person_id=person,
        provider_transport_id=transport,
        **fields,
    )


def save(client, card, **data):
    payload = {"action": "save", "vehicle": "", "driver_name": "", "note": ""}
    payload.update(data)
    return client.post(detail_url(card), payload, follow=True)


@pytest.mark.django_db
def test_petroline_personal_page_offers_block_and_api_edit(logged_client):
    card = petroline_card(provider_label="Тестовий Т", driver_name="Тестовий Т")

    response = logged_client.get(detail_url(card))

    html = response.content.decode()
    caps = response.context["caps"]
    assert caps["can_toggle_status"] is True
    assert (caps["mode_rename"], caps["edit_vehicle"], caps["type_name"]) == (True, True, "Персональна")
    assert caps["label_mismatch"] is False
    assert "ім'я водія «Авто | ПІБ»" in html
    assert "Тестовий Т" in html and "id 501" in html
    assert "disabled>Заблокувати" not in html


@pytest.mark.django_db
def test_petroline_unknown_type_is_not_written(logged_client, fake_petroline):
    """Картка, тип якої ще не оновлено з Petroline (як 512253 до «Оновити карти»), у Petroline не пишеться."""
    card = petroline_card(card_type=None, person="", vehicle="OLD")

    page = logged_client.get(detail_url(card))
    response = save(logged_client, card, vehicle="NEW")

    assert page.context["caps"]["mode_none"] is True
    assert "невідомий" in page.content.decode()
    assert fake_petroline.calls == []
    card.refresh_from_db()
    assert card.vehicle == "OLD"
    assert "Оновити карти Petroline" in response.content.decode()


@pytest.mark.django_db
def test_petroline_edit_disabled_is_local_only(logged_client, fake_petroline):
    card = petroline_card()
    ProviderConfig.objects.filter(code="petroline").update(edit_enabled=False)

    save(logged_client, card, vehicle="CC3333CC")

    assert fake_petroline.calls == []
    card.refresh_from_db()
    assert card.vehicle == "CC3333CC"
    assert AuditLog.objects.get().via_api is False


@pytest.mark.django_db
def test_petroline_personal_edit_goes_to_api_then_db(logged_client, fake_petroline):
    card = petroline_card(driver_name="Іваненко Іван", provider_label="Іваненко Іван")
    fake_petroline.result = InfoResult(1, "501", "", "AA0000AA | Іваненко Іван")

    save(logged_client, card, vehicle="AA0000AA", driver_name="Іваненко  Іван")

    assert fake_petroline.calls == [
        ("ID-1", {"vehicle": "AA0000AA", "driver_name": "Іваненко Іван", "expected": (1, "501")})
    ]
    card.refresh_from_db()
    assert (card.vehicle, card.driver_name, card.provider_label) == (
        "AA0000AA",
        "Іваненко Іван",
        "AA0000AA | Іваненко Іван",
    )
    entry = AuditLog.objects.get()
    assert (entry.via_api, entry.success) == (True, True)
    assert entry.changes["provider_label"] == ["Іваненко Іван", "AA0000AA | Іваненко Іван"]


@pytest.mark.django_db
def test_petroline_edit_matching_petroline_stays_local(logged_client, fake_petroline):
    """Виправили наші дані під Petroline — у Petroline писати нічого."""
    card = petroline_card(vehicle="Іваненко Іван", provider_label="Іваненко Іван")

    save(logged_client, card, vehicle="", driver_name="Іваненко Іван")

    assert fake_petroline.calls == []
    card.refresh_from_db()
    assert (card.vehicle, card.driver_name) == ("", "Іваненко Іван")
    assert AuditLog.objects.get().via_api is False


@pytest.mark.django_db
def test_petroline_api_error_keeps_db(logged_client, fake_petroline):
    card = petroline_card(vehicle="OLD", provider_label="OLD")
    fake_petroline.fail_with = PetrolineError("Petroline /api/Persons/update: HTTP 500")

    response = save(logged_client, card, vehicle="NEW")

    card.refresh_from_db()
    assert card.vehicle == "OLD"
    assert AuditLog.objects.get().success is False
    assert "зміни не збережено" in response.content.decode()


@pytest.mark.django_db
def test_shared_person_cards_are_updated_and_audited(logged_client, fake_petroline):
    card = petroline_card("ID-1", vehicle="OLD")
    other = petroline_card("ID-2", vehicle="OLD", ext_number="010002")
    blocked = petroline_card("ID-3", status=CardStatus.BLOCKED)
    stranger = petroline_card("ID-4", person="999", vehicle="KEEP")
    fake_petroline.result = InfoResult(1, "501", "", "AA0000AA | Іваненко Іван")

    save(logged_client, card, vehicle="AA0000AA", driver_name="Іваненко Іван")

    for c in (other, blocked):
        c.refresh_from_db()
        assert (c.vehicle, c.driver_name, c.provider_label) == (
            "AA0000AA",
            "Іваненко Іван",
            "AA0000AA | Іваненко Іван",
        )
    stranger.refresh_from_db()
    assert stranger.vehicle == "KEEP"
    assert AuditLog.objects.filter(action=AuditLog.Action.CARD_INFO).count() == 3
    assert len(fake_petroline.calls) == 1  # у Petroline це один запис водія


@pytest.mark.django_db
def test_shared_person_warning_lists_other_cards(logged_client):
    card = petroline_card("ID-1")
    petroline_card("ID-2", ext_number="010002", vehicle="AA1111AA")
    petroline_card("ID-3", status=CardStatus.BLOCKED)

    response = logged_client.get(detail_url(card))

    html = response.content.decode()
    caps = response.context["caps"]
    assert caps["shared_active"] == 1
    assert [c.number for c in caps["shared_cards"]] == ["ID-2", "ID-3"]
    assert "Водій у Petroline спільний для 3 карток" in html
    assert "010002" in html and "AA1111AA" in html
    assert 'data-shared="010002, ID-3"' in html


@pytest.mark.django_db
def test_tech_card_keeps_vehicle_and_edits_driver(logged_client, fake_petroline):
    card = petroline_card(card_type=3, vehicle="KEEP")
    fake_petroline.result = InfoResult(3, "501", "", "Технічний Пролив")

    response = logged_client.post(
        detail_url(card), {"action": "save", "driver_name": "Технічний Пролив", "note": ""}, follow=True
    )  # поле Авто вимкнене і не надсилається

    assert response.status_code == 200
    card.refresh_from_db()
    assert (card.vehicle, card.driver_name) == ("KEEP", "Технічний Пролив")
    assert fake_petroline.calls[0][1]["vehicle"] == "KEEP"


@pytest.mark.django_db
def test_vehicle_card_save_is_local_only(logged_client, fake_petroline):
    card = petroline_card(card_type=5, person="", transport="3001", provider_label="FORD AI17-51KI")

    page = logged_client.get(detail_url(card))
    logged_client.post(
        detail_url(card), {"action": "save", "vehicle": "AA0000AA", "driver_name": "Іваненко Іван"}
    )

    assert page.context["caps"]["mode_link"] is True
    assert "Зберегти і записати в Petroline" in page.content.decode()
    assert fake_petroline.calls == []
    card.refresh_from_db()
    assert (card.vehicle, card.driver_name, card.provider_card_type) == ("AA0000AA", "Іваненко Іван", 5)


@pytest.mark.django_db
def test_vehicle_card_push_links_and_stores_new_binding(logged_client, fake_petroline):
    card = petroline_card(card_type=5, person="", transport="3001")
    fake_petroline.result = InfoResult(1, "700", "", "AA0000AA | Іваненко Іван", action="linked")

    logged_client.post(
        detail_url(card), {"action": "push", "vehicle": "AA0000AA", "driver_name": "Іваненко Іван"}
    )

    assert fake_petroline.calls == [
        ("ID-1", {"vehicle": "AA0000AA", "driver_name": "Іваненко Іван", "expected": (5, "")})
    ]
    card.refresh_from_db()
    assert (card.provider_card_type, card.provider_person_id, card.provider_transport_id) == (1, "700", "")
    assert "прив'язано до наявного водія Petroline (id 700)" in str(AuditLog.objects.get().changes)


@pytest.mark.django_db
def test_vehicle_card_shows_existing_person_with_same_name(logged_client):
    petroline_card(
        "OLD-1", person="700", provider_label="AA0000AA | Іваненко Іван", status=CardStatus.BLOCKED
    )
    card = petroline_card("NEW-1", card_type=5, person="", vehicle="AA0000AA", driver_name="Іваненко Іван")

    caps = logged_client.get(detail_url(card)).context["caps"]

    assert [c.number for c in caps["same_name_cards"]] == ["OLD-1"]
    assert "буде прив'язано до нього" in caps["push_confirm"]


@pytest.mark.django_db
def test_label_mismatch_offers_take_and_push(logged_client, fake_petroline):
    card = petroline_card(vehicle="AA0000AA", driver_name="Тестовий Т", provider_label="Тестовий Т")

    response = logged_client.get(detail_url(card))

    caps = response.context["caps"]
    assert caps["label_mismatch"] is True
    assert (caps["provider_label"], caps["expected_label"]) == ("Тестовий Т", "AA0000AA | Тестовий Т")
    assert caps["from_petroline"] == ("Тестовий Т", "")
    assert "«Тестовий Т» → «AA0000AA | Тестовий Т»" in caps["push_confirm"]
    html = response.content.decode()
    assert "Взяти з Petroline" in html and "Записати наше в Petroline" in html


@pytest.mark.django_db
def test_take_from_petroline_is_local_and_audited(logged_client, fake_petroline):
    card = petroline_card(vehicle="Інше ім'я", provider_label="AA0000AA | Тестовий Т")

    logged_client.post(detail_url(card), {"action": "take"})

    card.refresh_from_db()
    assert (card.vehicle, card.driver_name) == ("AA0000AA", "Тестовий Т")
    entry = AuditLog.objects.get()
    assert entry.via_api is False
    assert fake_petroline.calls == []


@pytest.mark.django_db
def test_push_writes_our_unchanged_values(logged_client, fake_petroline):
    card = petroline_card(vehicle="AA0000AA", driver_name="Тестовий Т", provider_label="Тестовий Т")
    fake_petroline.result = InfoResult(1, "501", "", "AA0000AA | Тестовий Т")

    logged_client.post(detail_url(card), {"action": "push"})

    assert fake_petroline.calls[0][1]["vehicle"] == "AA0000AA"
    card.refresh_from_db()
    assert card.provider_label == "AA0000AA | Тестовий Т"
    assert AuditLog.objects.get().changes == {"provider_label": ["Тестовий Т", "AA0000AA | Тестовий Т"]}


@pytest.mark.django_db
def test_cards_list_inline_edit_refuses_petroline(logged_client, fake_petroline):
    card = petroline_card()

    response = logged_client.post(
        reverse("card_info", args=[card.pk]),
        data='{"vehicle": "X", "driver_name": "Y"}',
        content_type="application/json",
    )

    assert response.status_code == 400
    assert "на сторінці картки" in response.json()["error"]
    assert fake_petroline.calls == []


@pytest.mark.django_db
def test_cards_list_shows_type_and_mismatch(logged_client):
    petroline_card("ID-1", vehicle="Інше", provider_label="Тестовий Т")
    petroline_card("ID-2", person="502", driver_name="Інший І", provider_label="Інший І")

    html = logged_client.get(reverse("cards")).content.decode()

    assert "Персональна" in html
    assert "Розбіжності з Petroline: 1" in html
    assert html.count('data-mismatch="1"') == 1


@pytest.mark.django_db
def test_duplicates_page_groups_active_shared_cards(logged_client):
    petroline_card("ID-1", provider_label="AA0000AA | Іваненко Іван")
    petroline_card("ID-2")
    petroline_card("ID-3", status=CardStatus.BLOCKED)
    petroline_card("ID-4", person="777")  # один активний — не дубль
    petroline_card("ID-5", person="888")
    petroline_card("ID-6", person="888", status=CardStatus.BLOCKED)  # заміна — не дубль

    response = logged_client.get(reverse("card_duplicates"))

    groups = response.context["groups"]
    assert [g["person_id"] for g in groups] == ["501"]
    assert [c.number for c in groups[0]["cards"]] == ["ID-1", "ID-2", "ID-3"]
    assert "AA0000AA | Іваненко Іван" in response.content.decode()
    assert "Дублі водіїв Petroline: 1" in logged_client.get(reverse("cards")).content.decode()


@pytest.mark.django_db
def test_sync_petroline_cards_fills_only_empty_cards(monkeypatch):
    ProviderConfig.ensure_all()
    Card.objects.create(provider="petroline", number="ID-2", vehicle="OURS")
    row = {
        "ext_number": "",
        "external_id": "1",
        "api_status": 3,
        "status": CardStatus.ACTIVE,
        "card_type": 1,
        "person_id": "501",
        "transport_id": "",
        "label": "Тестовий Т",
        "vehicle": "Тестовий Т",
        "driver_name": "",
    }
    rows = [{**row, "number": number} for number in ("ID-1", "ID-2")]
    monkeypatch.setattr(card_service.PetrolineCards, "list_cards", lambda self: rows)

    card_service.sync_petroline_cards()

    new, ours = Card.objects.get(number="ID-1"), Card.objects.get(number="ID-2")
    assert (new.vehicle, new.driver_name, new.provider_person_id) == ("Тестовий Т", "", "501")
    # у нас уже щось є — не чіпаємо, лише запам'ятовуємо, як у Petroline (розбіжність видно на сторінці)
    assert (ours.vehicle, ours.driver_name, ours.provider_label) == ("OURS", "", "Тестовий Т")


@pytest.mark.django_db
def test_block_and_unblock(logged_client, ukrnafta_card, fake_api):
    logged_client.post(detail_url(ukrnafta_card), {"action": "status", "status": CardStatus.BLOCKED})
    ukrnafta_card.refresh_from_db()
    assert ukrnafta_card.status == CardStatus.BLOCKED

    response = logged_client.post(
        detail_url(ukrnafta_card), {"action": "status", "status": CardStatus.ACTIVE}, follow=True
    )
    ukrnafta_card.refresh_from_db()
    assert ukrnafta_card.status == CardStatus.ACTIVE
    assert "Картку розблоковано" in response.content.decode()

    assert [c[1] for c in fake_api.calls] == [{"status": CardStatus.BLOCKED}, {"status": CardStatus.ACTIVE}]
    entries = list(AuditLog.objects.order_by("id"))
    assert [e.changes["status"] for e in entries] == [[0, 1], [1, 0]]
    assert "Статус: «Активна» → «Заблокована»" in entries[0].changes_display()


@pytest.mark.django_db
def test_block_petroline_goes_to_api_then_db_and_audit(logged_client, fake_api):
    ProviderConfig.ensure_all()
    card = Card.objects.create(provider="petroline", number="ID-1", status=CardStatus.ACTIVE)

    logged_client.post(detail_url(card), {"action": "status", "status": CardStatus.BLOCKED})

    card.refresh_from_db()
    assert card.status == CardStatus.BLOCKED
    assert fake_api.calls == [("ID-1", {"status": CardStatus.BLOCKED})]
    entry = AuditLog.objects.get()
    assert (entry.provider, entry.via_api, entry.success) == ("petroline", True, True)


@pytest.mark.django_db
def test_block_petroline_api_error_keeps_status(logged_client, fake_api):
    ProviderConfig.ensure_all()
    card = Card.objects.create(provider="petroline", number="ID-1", status=CardStatus.ACTIVE)
    fake_api.fail_with = PetrolineError("Petroline Keys/update: HTTP 500")

    logged_client.post(detail_url(card), {"action": "status", "status": CardStatus.BLOCKED})

    card.refresh_from_db()
    assert card.status == CardStatus.ACTIVE
    entry = AuditLog.objects.get()
    assert entry.success is False
    assert "HTTP 500" in entry.error


@pytest.mark.django_db
def test_block_petroline_with_edit_disabled_is_refused(logged_client, fake_api):
    ProviderConfig.ensure_all()
    ProviderConfig.objects.filter(code="petroline").update(edit_enabled=False)
    card = Card.objects.create(provider="petroline", number="ID-1", status=CardStatus.ACTIVE)

    response = logged_client.post(
        detail_url(card), {"action": "status", "status": CardStatus.BLOCKED}, follow=True
    )

    card.refresh_from_db()
    assert card.status == CardStatus.ACTIVE
    assert fake_api.calls == []
    assert "режим редагування вимкнений" in response.content.decode()
    assert AuditLog.objects.count() == 0


@pytest.mark.django_db
def test_block_api_error_keeps_status(logged_client, ukrnafta_card, fake_api):
    fake_api.fail_with = UkrnaftaError("HTTP 403: denied")

    logged_client.post(detail_url(ukrnafta_card), {"action": "status", "status": CardStatus.BLOCKED})

    ukrnafta_card.refresh_from_db()
    assert ukrnafta_card.status == CardStatus.ACTIVE
    assert AuditLog.objects.get().success is False


# --- аудит дій зі сторінки «Карти» --------------------------------------------------------------


@pytest.mark.django_db
def test_create_and_delete_are_audited(logged_client):
    ProviderConfig.ensure_all()
    created = logged_client.post(
        reverse("card_create"),
        {"provider": "upg", "number": "U-1", "vehicle": "DD4444DD"},
        content_type="application/json",
    ).json()

    logged_client.post(reverse("card_delete", args=[created["id"]]))

    entries = list(AuditLog.objects.order_by("id"))
    assert [e.action for e in entries] == [AuditLog.Action.CARD_CREATE, AuditLog.Action.CARD_DELETE]
    assert all(e.username == "manager" for e in entries)
    assert entries[1].card is None and "U-1" in entries[1].card_label


@pytest.mark.django_db
def test_page_shows_audit_entries(logged_client, ukrnafta_card):
    logged_client.post(
        detail_url(ukrnafta_card),
        {"action": "save", "vehicle": "AA1111AA", "driver_name": "Іваненко Іван", "note": "перевірка"},
    )

    html = logged_client.get(detail_url(ukrnafta_card)).content.decode()

    assert "Журнал змін" in html
    assert "manager" in html
    assert "Примітка: «» → «перевірка»" in html
