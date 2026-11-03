"""Синхронізація вибраних провайдерів за глибиною або періодом з конфігурації.

Реальні API не викликаються: підміняється fuel.services.run_sync.
"""

from datetime import date, timedelta
from io import StringIO

import pytest
from django.contrib.auth import get_user_model
from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.urls import reverse
from django.utils import timezone

from fuel import services
from fuel.models import ProviderConfig
from fuel.services import (
    MODE_DAYS,
    MODE_PERIOD,
    build_periods,
    split_period,
    sync_configs,
    sync_periods_chunked,
)


class FakeRunSync:
    """Записує виклики run_sync; для провайдерів з errors повертає помилку."""

    def __init__(self, errors: set[str] | None = None, result: tuple[int, int] = (1, 0)):
        self.calls: list[tuple[str, date, date]] = []
        self.errors = errors or set()
        self.result = result

    def __call__(self, names, date_from, date_to):
        name = names[0]
        self.calls.append((name, date_from, date_to))
        if name in self.errors:
            return {name: "error: API недоступне"}
        return {name: self.result}


@pytest.fixture
def fake_sync(monkeypatch):
    fake = FakeRunSync()
    monkeypatch.setattr(services, "run_sync", fake)
    monkeypatch.setattr(services.time, "sleep", lambda _seconds: None)
    return fake


# --- розбиття на частини -------------------------------------------------------------------------


def test_split_short_period_is_single_chunk():
    assert split_period(date(2026, 10, 1), date(2026, 10, 30)) == [(date(2026, 10, 1), date(2026, 10, 30))]


def test_split_long_period_into_30_day_chunks():
    chunks = split_period(date(2026, 1, 1), date(2026, 3, 15))

    assert chunks == [
        (date(2026, 1, 1), date(2026, 1, 30)),
        (date(2026, 1, 31), date(2026, 3, 1)),
        (date(2026, 3, 2), date(2026, 3, 15)),
    ]


def test_split_single_day():
    assert split_period(date(2026, 5, 5), date(2026, 5, 5)) == [(date(2026, 5, 5), date(2026, 5, 5))]


# --- періоди з конфігурації ---------------------------------------------------------------------


@pytest.mark.django_db
def test_build_periods_days_mode_uses_each_config_depth():
    configs = ProviderConfig.ensure_all()
    configs["upg"].sync_days = 10
    configs["ukrnafta"].sync_days = 45
    today = date(2026, 11, 3)

    periods, messages = build_periods([configs["upg"], configs["ukrnafta"]], MODE_DAYS, today)

    assert messages == []
    assert periods == {
        "upg": (date(2026, 10, 24), today),
        "ukrnafta": (date(2026, 9, 19), today),
    }


@pytest.mark.django_db
def test_build_periods_skips_inactive_with_warning():
    configs = ProviderConfig.ensure_all()
    configs["upg"].is_active = False

    periods, messages = build_periods([configs["upg"], configs["ukrnafta"]], MODE_DAYS)

    assert list(periods) == ["ukrnafta"]
    assert messages[0].level == "warning"
    assert "upg" in messages[0].text and "неактивний" in messages[0].text


@pytest.mark.django_db
def test_build_periods_period_mode_requires_dates():
    config = ProviderConfig.ensure_all()["upg"]

    periods, messages = build_periods([config], MODE_PERIOD)

    assert periods == {}
    assert messages[0].level == "error"
    assert "не задано період" in messages[0].text


@pytest.mark.django_db
def test_build_periods_period_mode_rejects_reversed_dates():
    config = ProviderConfig.ensure_all()["upg"]
    config.sync_date_from = date(2026, 10, 10)
    config.sync_date_to = date(2026, 10, 1)

    periods, messages = build_periods([config], MODE_PERIOD)

    assert periods == {}
    assert "пізніша" in messages[0].text


@pytest.mark.django_db
def test_model_clean_validates_period():
    config = ProviderConfig.ensure_all()["upg"]
    config.sync_date_from = date(2026, 10, 1)
    config.sync_date_to = None

    with pytest.raises(ValidationError):
        config.full_clean()

    config.sync_date_to = timezone.localdate() + timedelta(days=1)
    with pytest.raises(ValidationError):
        config.full_clean()


# --- запуск частинами ---------------------------------------------------------------------------


def test_chunked_sync_pauses_between_requests(monkeypatch):
    fake = FakeRunSync(result=(2, 1))
    monkeypatch.setattr(services, "run_sync", fake)
    pauses: list[float] = []

    messages = sync_periods_chunked(
        {"upg": (date(2026, 1, 1), date(2026, 3, 15))}, pause=2.0, sleep=pauses.append
    )

    assert [call[1:] for call in fake.calls] == split_period(date(2026, 1, 1), date(2026, 3, 15))
    assert pauses == [2.0, 2.0]  # між трьома запитами — дві паузи
    texts = [m.text for m in messages]
    assert "розбито на 3 частин" in texts[0]
    assert "частина 1/3" in texts[1]
    assert messages[-1].level == "success"
    assert "нових 6, оновлено 3" in messages[-1].text


def test_chunk_error_stops_provider_but_not_others(monkeypatch):
    fake = FakeRunSync(errors={"upg"})
    monkeypatch.setattr(services, "run_sync", fake)

    messages = sync_periods_chunked(
        {
            "upg": (date(2026, 1, 1), date(2026, 3, 15)),
            "ukrnafta": (date(2026, 3, 1), date(2026, 3, 15)),
        },
        sleep=lambda _s: None,
    )

    called = [call[0] for call in fake.calls]
    assert called == ["upg", "ukrnafta"]  # після помилки решта частин upg не запитується
    errors = [m for m in messages if m.level == "error"]
    assert len(errors) == 1 and "upg" in errors[0].text and "решту частин пропущено" in errors[0].text
    assert messages[-1].level == "success" and "ukrnafta" in messages[-1].text


@pytest.mark.django_db
def test_sync_configs_without_providers_warns(fake_sync):
    config = ProviderConfig.ensure_all()["upg"]
    config.is_active = False

    messages = sync_configs([config], MODE_DAYS)

    assert fake_sync.calls == []
    assert messages[-1].text == "Немає провайдерів для синхронізації"


# --- адмінка ------------------------------------------------------------------------------------


@pytest.fixture
def admin_client_logged(client):
    user = get_user_model().objects.create_superuser("admin", "admin@example.com", "pass")
    client.force_login(user)
    return client


def run_admin_action(client, action: str, codes: list[str]):
    ids = [str(ProviderConfig.objects.get(code=code).pk) for code in codes]
    url = reverse("admin:fuel_providerconfig_changelist")
    response = client.post(url, {"action": action, "_selected_action": ids}, follow=True)
    return [str(m) for m in get_messages(response.wsgi_request)]


@pytest.mark.django_db
def test_admin_sync_by_days_action(admin_client_logged, fake_sync):
    ProviderConfig.ensure_all()
    ProviderConfig.objects.filter(code="upg").update(sync_days=5)
    ProviderConfig.objects.filter(code="petroline").update(is_active=False)

    texts = run_admin_action(admin_client_logged, "sync_by_days", ["upg", "petroline"])

    today = timezone.localdate()
    assert fake_sync.calls == [("upg", today - timedelta(days=5), today)]
    assert any("petroline" in t and "неактивний" in t for t in texts)
    assert any(t.startswith("upg") and "нових 1" in t for t in texts)


@pytest.mark.django_db
def test_admin_sync_by_period_action_reports_missing_period(admin_client_logged, fake_sync):
    ProviderConfig.ensure_all()
    ProviderConfig.objects.filter(code="ukrnafta").update(
        sync_date_from=date(2026, 1, 1), sync_date_to=date(2026, 2, 15)
    )

    texts = run_admin_action(admin_client_logged, "sync_by_period", ["ukrnafta", "upg"])

    assert [call[0] for call in fake_sync.calls] == ["ukrnafta", "ukrnafta"]
    assert any("upg" in t and "не задано період" in t for t in texts)
    assert any("розбито на 2 частин" in t for t in texts)


# --- команда sync_fuel --------------------------------------------------------------------------


@pytest.mark.django_db
def test_command_config_days(fake_sync):
    ProviderConfig.ensure_all()
    ProviderConfig.objects.filter(code="upg").update(sync_days=7)
    out = StringIO()

    call_command("sync_fuel", "--config-days", "--provider", "upg", stdout=out)

    today = timezone.localdate()
    assert fake_sync.calls == [("upg", today - timedelta(days=7), today)]
    assert "нових 1" in out.getvalue()


@pytest.mark.django_db
def test_command_days_chunks_long_period(fake_sync):
    call_command("sync_fuel", "--days", "65", "--provider", "ukrnafta", stdout=StringIO())

    assert len(fake_sync.calls) == 3


@pytest.mark.django_db
def test_command_config_period_without_dates_shows_examples(fake_sync):
    with pytest.raises(CommandError) as exc:
        call_command("sync_fuel", "--config-period", "--provider", "upg", stdout=StringIO())

    assert "Приклади" in str(exc.value)
    assert fake_sync.calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["--days", "0"],
        ["--days", "abc"],
        ["--days", "2", "--config-days"],
        ["--provider", "unknown"],
    ],
)
@pytest.mark.django_db
def test_command_bad_arguments_show_examples(args, fake_sync):
    with pytest.raises(CommandError) as exc:
        call_command("sync_fuel", *args, stdout=StringIO())

    assert "python manage.py sync_fuel --days 2" in str(exc.value)
    assert fake_sync.calls == []
