"""Сторінка картки: редагування за провайдером, блокування, журнал аудиту.

API провайдерів підмінено фейковим клієнтом — реальних запитів немає.
"""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from fuel import card_service
from fuel.models import AuditLog, Card, CardStatus, ProviderConfig, Transaction
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
def test_petroline_page_is_local_only_without_block(logged_client):
    ProviderConfig.ensure_all()
    card = Card.objects.create(provider="petroline", number="ID-1", status=CardStatus.ACTIVE)

    response = logged_client.get(detail_url(card))

    html = response.content.decode()
    assert "змінюється у провайдера" not in html
    assert "disabled>Заблокувати" in html
    assert "лише читає" in html
    assert response.context["caps"]["can_toggle_status"] is False


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


@pytest.mark.django_db
def test_petroline_edit_is_local_only(logged_client, fake_api):
    ProviderConfig.ensure_all()
    card = Card.objects.create(provider="petroline", number="ID-1")

    logged_client.post(
        detail_url(card), {"action": "save", "vehicle": "CC3333CC", "driver_name": "", "note": ""}
    )

    assert fake_api.calls == []
    card.refresh_from_db()
    assert card.vehicle == "CC3333CC"
    assert AuditLog.objects.get().via_api is False


# --- блокування ---------------------------------------------------------------------------------


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
def test_block_petroline_is_refused(logged_client, fake_api):
    ProviderConfig.ensure_all()
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
