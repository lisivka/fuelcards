"""Зразкові тести: upsert транзакцій і розбір дат. Реальні API тут не викликаються."""

from datetime import datetime
from decimal import Decimal

import pytest
from django.utils import timezone

from fuel.models import Card, Transaction
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
