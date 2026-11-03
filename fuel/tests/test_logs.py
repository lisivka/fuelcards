"""Журнал аудиту синхронізацій і сторінка «Логи». Реальні API не викликаються."""

from datetime import date
from io import StringIO

import pytest
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from fuel import card_service, services
from fuel.models import AuditLog, Card, ProviderConfig
from fuel.services import sync_periods_chunked, sync_since_last


@pytest.fixture
def user(django_user_model):
    return django_user_model.objects.create_user(username="manager", password="x")


@pytest.fixture
def logged_client(client, user):
    client.force_login(user)
    return client


def fake_run_sync(errors: set[str] | None = None):
    errors = errors or set()

    def run(names, date_from, date_to):
        name = names[0]
        return {name: "error: API недоступне" if name in errors else (2, 1)}

    return run


# --- запис синхронізацій транзакцій -------------------------------------------------------------


@pytest.mark.django_db
def test_button_sync_records_each_active_provider(monkeypatch, user):
    ProviderConfig.ensure_all()
    ProviderConfig.objects.filter(code="upg").update(is_active=False)

    def run(names, date_from, date_to):
        name = names[0]
        if name == "upg":
            return {name: None}  # неактивний
        return fake_run_sync({"petroline"})(names, date_from, date_to)

    monkeypatch.setattr(services, "run_sync", run)

    sync_since_last(user=user)

    entries = {e.provider: e for e in AuditLog.objects.all()}
    assert set(entries) == {"petroline", "ukrnafta"}  # неактивний upg не записано
    ok = entries["ukrnafta"]
    assert ok.action == AuditLog.Action.TX_SYNC and ok.success and ok.username == "manager"
    assert ok.details["source"] == AuditLog.SOURCE_BUTTON
    assert (ok.details["created"], ok.details["updated"]) == (2, 1)
    assert ok.details["date_to"] == timezone.localdate().isoformat()
    failed = entries["petroline"]
    assert failed.success is False and "API недоступне" in failed.error


@pytest.mark.django_db
def test_chunked_sync_records_one_entry_per_provider(monkeypatch, user):
    monkeypatch.setattr(services, "run_sync", fake_run_sync())

    sync_periods_chunked(
        {"upg": (date(2026, 1, 1), date(2026, 3, 15))},
        sleep=lambda _s: None,
        user=user,
        source=AuditLog.SOURCE_ADMIN_PERIOD,
    )

    entry = AuditLog.objects.get()
    assert entry.details == {
        "source": AuditLog.SOURCE_ADMIN_PERIOD,
        "date_from": "2026-01-01",
        "date_to": "2026-03-15",
        "chunks": 3,
        "chunks_done": 3,
        "created": 6,
        "updated": 3,
    }
    lines = entry.details_display()
    assert "Період: 01.01.2026 .. 15.03.2026" in lines
    assert "Нових: 6, оновлено: 3" in lines
    assert "Частин: 3 з 3" in lines
    assert "Запуск: адмінка, за період з/по" in lines


@pytest.mark.django_db
def test_chunked_sync_error_is_recorded_with_partial_result(monkeypatch):
    calls = []

    def run(names, date_from, date_to):
        calls.append(date_from)
        return {names[0]: (1, 0) if len(calls) == 1 else "error: timeout"}

    monkeypatch.setattr(services, "run_sync", run)

    sync_periods_chunked({"upg": (date(2026, 1, 1), date(2026, 3, 15))}, sleep=lambda _s: None)

    entry = AuditLog.objects.get()
    assert entry.success is False and "timeout" in entry.error
    assert (entry.details["chunks_done"], entry.details["created"]) == (1, 1)
    assert entry.who == "система (команда)"


@pytest.mark.django_db
def test_admin_action_records_user_and_source(monkeypatch, admin_client):
    monkeypatch.setattr(services, "run_sync", fake_run_sync())
    monkeypatch.setattr(services.time, "sleep", lambda _s: None)
    ProviderConfig.ensure_all()
    pk = ProviderConfig.objects.get(code="ukrnafta").pk

    admin_client.post(
        reverse("admin:fuel_providerconfig_changelist"),
        {"action": "sync_by_days", "_selected_action": [pk]},
    )

    entry = AuditLog.objects.get()
    assert entry.username == "admin"
    assert entry.details["source"] == AuditLog.SOURCE_ADMIN_DAYS


@pytest.mark.django_db
def test_sync_fuel_command_records_system(monkeypatch):
    monkeypatch.setattr(services, "run_sync", fake_run_sync())

    call_command("sync_fuel", "--days", "1", "--provider", "upg", stdout=StringIO())

    entry = AuditLog.objects.get()
    assert entry.username == "" and entry.details["source"] == AuditLog.SOURCE_COMMAND


# --- запис оновлення карток ---------------------------------------------------------------------


@pytest.mark.django_db
def test_cards_sync_button_records_result(monkeypatch, logged_client):
    ProviderConfig.ensure_all()
    monkeypatch.setitem(card_service.SYNCERS, "ukrnafta", lambda: (3, 4))

    response = logged_client.post(
        reverse("cards_sync"), {"provider": "ukrnafta"}, content_type="application/json"
    )

    assert response.json() == {"created": 3, "updated": 4}
    entry = AuditLog.objects.get()
    assert entry.action == AuditLog.Action.CARDS_SYNC and entry.username == "manager"
    assert (entry.details["created"], entry.details["updated"]) == (3, 4)


@pytest.mark.django_db
def test_cards_sync_error_is_recorded(monkeypatch):
    ProviderConfig.ensure_all()

    def boom():
        raise RuntimeError("HTTP 401")

    monkeypatch.setitem(card_service.SYNCERS, "petroline", boom)

    with pytest.raises(RuntimeError):
        card_service.sync_cards("petroline")

    entry = AuditLog.objects.get()
    assert entry.success is False and entry.error == "HTTP 401"


@pytest.mark.django_db
def test_cards_sync_inactive_provider_is_not_recorded():
    ProviderConfig.ensure_all()
    ProviderConfig.objects.filter(code="petroline").update(is_active=False)

    with pytest.raises(ValueError):
        card_service.sync_cards("petroline")

    assert AuditLog.objects.count() == 0


@pytest.mark.django_db
def test_sync_cards_command(monkeypatch):
    ProviderConfig.ensure_all()
    monkeypatch.setitem(card_service.SYNCERS, "ukrnafta", lambda: (0, 5))
    out = StringIO()

    call_command("sync_cards", stdout=out)

    assert "нових 0, оновлено 5" in out.getvalue()
    assert AuditLog.objects.get().details["source"] == AuditLog.SOURCE_COMMAND


# --- сторінка «Логи» ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_logs_requires_login(client):
    response = client.get(reverse("logs"))

    assert response.status_code == 302 and "login" in response.url


def make_entries():
    card = Card.objects.create(provider="ukrnafta", number="0000111122223333")
    AuditLog.objects.create(
        action=AuditLog.Action.CARD_INFO,
        username="manager",
        card=card,
        card_label=str(card),
        provider="ukrnafta",
        changes={"vehicle": ["", "AA1111AA"]},
    )
    AuditLog.objects.create(
        action=AuditLog.Action.TX_SYNC,
        provider="upg",
        details={"source": "command", "created": 1, "updated": 0},
    )
    AuditLog.objects.create(
        action=AuditLog.Action.TX_SYNC,
        provider="petroline",
        success=False,
        error="HTTP 500",
        details={"source": "button"},
    )
    return card


@pytest.mark.django_db
def test_logs_page_shows_all_entries(logged_client):
    card = make_entries()

    response = logged_client.get(reverse("logs"))

    html = response.content.decode()
    assert response.context["page"].paginator.count == 3
    assert reverse("card_detail", args=[card.pk]) + "?back=logs" in html
    assert "Авто: «» → «AA1111AA»" in html
    assert "система (команда)" in html
    assert "HTTP 500" in html
    assert 'href="/logs/"' in html  # пункт меню


@pytest.mark.parametrize(
    "params, providers",
    [
        ({"kind": "cards"}, {"ukrnafta"}),
        ({"kind": "sync"}, {"upg", "petroline"}),
        ({"provider": "upg"}, {"upg"}),
        ({"errors": "1"}, {"petroline"}),
        ({"kind": "bogus", "provider": "bogus"}, {"ukrnafta", "upg", "petroline"}),
    ],
)
@pytest.mark.django_db
def test_logs_filters(logged_client, params, providers):
    make_entries()

    page = logged_client.get(reverse("logs"), params).context["page"]

    assert {e.provider for e in page} == providers


@pytest.mark.django_db
def test_logs_pagination_20_per_page(logged_client):
    AuditLog.objects.bulk_create(
        AuditLog(action=AuditLog.Action.TX_SYNC, provider="upg", details={"n": i}) for i in range(45)
    )

    response = logged_client.get(reverse("logs"), {"kind": "sync", "page": "3"})

    page = response.context["page"]
    assert page.paginator.num_pages == 3
    assert len(page.object_list) == 5
    assert "kind=sync&page=1" in response.content.decode()


@pytest.mark.django_db
def test_card_page_back_to_logs(logged_client):
    card = Card.objects.create(provider="upg", number="U-1")

    response = logged_client.get(reverse("card_detail", args=[card.pk]), {"back": "logs"})

    assert response.context["back_url"] == reverse("logs")
    assert "← Логи" in response.content.decode()


def test_details_display_without_period():
    entry = AuditLog(
        action=AuditLog.Action.CARDS_SYNC, details={"source": "button", "created": 0, "updated": 2}
    )

    assert entry.details_display() == ["Нових: 0, оновлено: 2", "Запуск: кнопка на сторінці"]


def test_old_dates_are_formatted():
    entry = AuditLog(details={"date_from": date(2026, 1, 2).isoformat(), "date_to": "bad"})

    assert entry.details_display()[0] == "Період: 02.01.2026 .. bad"
