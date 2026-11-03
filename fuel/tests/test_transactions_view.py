"""Сторінка «Транзакції»: швидкі періоди, к-сть рядків, пагінація, повернення після синхронізації."""

from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from django.urls import reverse
from django.utils import timezone

from fuel import views
from fuel.models import Transaction
from fuel.views import DEFAULT_PER_PAGE, parse_per_page, preset_dates

TODAY = date(2026, 11, 3)


@pytest.fixture
def logged_client(client, django_user_model):
    user = django_user_model.objects.create_user(username="manager", password="x")
    client.force_login(user)
    return client


def make_transactions(count: int, day: date, provider: str = "upg") -> None:
    ts = timezone.make_aware(datetime(day.year, day.month, day.day, 12, 0))
    Transaction.objects.bulk_create(
        Transaction(provider=provider, external_id=f"{provider}-{day}-{i}", timestamp=ts)
        for i in range(count)
    )


# --- допоміжні функції --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code, expected",
    [
        ("today", (TODAY, TODAY)),
        ("yesterday", (date(2026, 11, 2), date(2026, 11, 2))),
        ("7d", (date(2026, 10, 28), TODAY)),
        ("30d", (date(2026, 10, 5), TODAY)),
        ("bogus", None),
        ("", None),
    ],
)
def test_preset_dates(code, expected):
    assert preset_dates(code, TODAY) == expected


@pytest.mark.parametrize(
    "value, expected",
    [("25", 25), ("500", 500), ("100", 100), ("7", DEFAULT_PER_PAGE), ("abc", DEFAULT_PER_PAGE), (None, 100)],
)
def test_parse_per_page(value, expected):
    assert parse_per_page(value) == expected


# --- сторінка -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_period_overrides_dates_and_keeps_filters(logged_client):
    today = timezone.localdate()
    make_transactions(2, today - timedelta(days=3))
    make_transactions(1, today - timedelta(days=3), provider="ukrnafta")
    make_transactions(1, today - timedelta(days=10))

    response = logged_client.get(
        reverse("transactions"), {"period": "7d", "from": "2020-01-01", "to": "2020-01-01", "provider": "upg"}
    )

    ctx = response.context
    assert (ctx["d_from"], ctx["d_to"]) == (today - timedelta(days=6), today)
    assert ctx["page"].paginator.count == 2
    query = parse_qs(ctx["query"])
    assert query["period"] == ["7d"] and "from" not in query
    assert query["provider"] == ["upg"]
    assert 'data-period="7d" class="active"' in response.content.decode()


@pytest.mark.django_db
def test_manual_dates_without_period(logged_client):
    response = logged_client.get(reverse("transactions"), {"from": "2026-10-01", "to": "2026-10-05"})

    ctx = response.context
    assert ctx["period"] == ""
    assert (ctx["d_from"], ctx["d_to"]) == (date(2026, 10, 1), date(2026, 10, 5))
    assert parse_qs(ctx["query"])["from"] == ["2026-10-01"]


@pytest.mark.django_db
def test_per_page_and_elided_pagination(logged_client):
    make_transactions(25 * 22, timezone.localdate())

    response = logged_client.get(reverse("transactions"), {"period": "today", "per_page": "25", "page": "10"})

    ctx = response.context
    assert ctx["per_page"] == 25
    assert ctx["page"].paginator.num_pages == 22
    ellipsis = ctx["ellipsis"]
    assert ctx["page_range"] == [1, 2, 3, ellipsis, 8, 9, 10, 11, 12, ellipsis, 20, 21, 22]
    html = response.content.decode()
    assert '<span class="current" aria-current="page">10</span>' in html
    assert "per_page=25&page=22" in html


@pytest.mark.django_db
def test_invalid_per_page_falls_back_to_default(logged_client):
    response = logged_client.get(reverse("transactions"), {"per_page": "100000"})

    assert response.context["per_page"] == DEFAULT_PER_PAGE


@pytest.mark.django_db
def test_single_page_has_no_pager(logged_client):
    make_transactions(3, timezone.localdate())

    html = logged_client.get(reverse("transactions"), {"period": "today"}).content.decode()

    assert 'class="pager"' not in html


@pytest.mark.django_db
def test_sync_returns_to_same_report(logged_client, monkeypatch):
    monkeypatch.setattr(views, "sync_since_last", lambda **_kwargs: ({}, []))

    response = logged_client.post(
        reverse("transactions_sync"), {"query": "period=30d&provider=upg&per_page=50&page=3&evil=1"}
    )

    params = parse_qs(urlsplit(response.url).query)
    assert params == {"period": ["30d"], "provider": ["upg"], "per_page": ["50"], "page": ["3"]}
