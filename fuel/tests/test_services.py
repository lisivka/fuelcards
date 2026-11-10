"""Upsert транзакцій, розбір дат, ізоляція провайдерів у run_sync. Реальні API тут не викликаються."""

from datetime import date, datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from fuel import services
from fuel.models import Card, ProviderConfig, Transaction
from fuel.providers.base import NormalizedTx, parse_iso
from fuel.services import save_transactions


def make_tx(**overrides) -> NormalizedTx:
    data = dict(
        provider="ukrnafta",
        external_id="TX-1",
        timestamp=timezone.make_aware(datetime(2026, 10, 1, 9, 30)),
        card_ref="0000111122223333",
        vehicle="АА0000АА",
        fuel="ДП",
        liters=Decimal("40.00"),
        price=Decimal("55.00"),
        amount=Decimal("2200.00"),
        station="АЗС тестова",
    )
    data.update(overrides)
    return NormalizedTx(**data)


@pytest.mark.django_db
def test_new_transaction_is_created_with_card():
    created, updated = save_transactions([make_tx()])

    assert (created, updated) == (1, 0)
    tx = Transaction.objects.get(provider="ukrnafta", external_id="TX-1")
    assert tx.card is not None
    assert tx.card.number == "0000111122223333"


@pytest.mark.django_db
def test_repeat_sync_does_not_duplicate():
    save_transactions([make_tx()])
    created, updated = save_transactions([make_tx()])

    assert (created, updated) == (0, 1)
    assert Transaction.objects.count() == 1
    assert Card.objects.count() == 1


@pytest.mark.django_db
def test_cancellation_updates_existing_transaction():
    save_transactions([make_tx()])
    save_transactions([make_tx(canceled=True)])

    assert Transaction.objects.get(external_id="TX-1").canceled is True


@pytest.mark.parametrize(
    "raw",
    ["2026-10-01T09:30:50.08", "2026-10-01T09:30:50Z", "2026-10-01T09:30:50.1234567"],
)
def test_parse_iso_accepts_provider_formats_on_py310(raw):
    assert parse_iso(raw).year == 2026


@pytest.mark.django_db
def test_sync_keeps_our_card_data():
    """Синхронізація не перетирає наше Авто картки: дані провайдера — лише у vehicle_raw транзакції."""
    save_transactions([make_tx()])
    card = Card.objects.get()
    card.vehicle = "Наше авто"
    card.save()

    save_transactions([make_tx(vehicle="Від провайдера", external_id="TX-2")])

    card.refresh_from_db()
    assert card.vehicle == "Наше авто"
    assert Transaction.objects.get(external_id="TX-2").vehicle_raw == "Від провайдера"


@pytest.mark.django_db
def test_transaction_without_card_ref_has_no_card():
    save_transactions([make_tx(card_ref="")])

    assert Transaction.objects.get().card is None
    assert Card.objects.count() == 0


# --- run_sync: ізоляція провайдерів -------------------------------------------------------------


class GoodProvider:
    def fetch(self, date_from, date_to):
        return [make_tx(provider="upg", external_id="U-1")]


class BrokenProvider:
    def fetch(self, date_from, date_to):
        raise RuntimeError("API недоступне")


@pytest.mark.django_db
def test_run_sync_failure_of_one_provider_does_not_stop_others(monkeypatch, caplog):
    monkeypatch.setattr(services, "PROVIDERS", {"ukrnafta": BrokenProvider, "upg": GoodProvider})
    ProviderConfig.ensure_all()

    results = services.run_sync(["ukrnafta", "upg"], date(2026, 10, 1), date(2026, 10, 1))

    assert results["ukrnafta"] == "error: API недоступне"
    assert results["upg"] == (1, 0)
    assert Transaction.objects.filter(provider="upg").count() == 1
    assert "Sync failed for ukrnafta" in caplog.text


@pytest.mark.django_db
def test_run_sync_skips_inactive_provider(monkeypatch):
    monkeypatch.setattr(services, "PROVIDERS", {"ukrnafta": BrokenProvider, "upg": GoodProvider})
    ProviderConfig.ensure_all()
    ProviderConfig.objects.filter(code="ukrnafta").update(is_active=False)

    results = services.run_sync(["ukrnafta", "upg"], date(2026, 10, 1), date(2026, 10, 1))

    assert results == {"ukrnafta": None, "upg": (1, 0)}
