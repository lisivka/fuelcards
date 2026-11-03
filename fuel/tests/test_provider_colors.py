"""Кольори рядків за провайдером і колонка «Ідентифікатор» на сторінці карток."""

import pytest
from django.core.exceptions import ValidationError
from django.urls import reverse
from django.utils import timezone

from fuel.models import DEFAULT_ROW_COLORS, Card, ProviderConfig, Transaction


@pytest.mark.django_db
def test_ensure_all_sets_default_colors():
    configs = ProviderConfig.ensure_all()

    for code, color in DEFAULT_ROW_COLORS.items():
        assert configs[code].row_color == color


@pytest.mark.django_db
def test_admin_color_is_kept_by_ensure_all():
    ProviderConfig.objects.create(code="upg", row_color="#123456")

    assert ProviderConfig.ensure_all()["upg"].row_color == "#123456"
    assert ProviderConfig.row_colors()["upg"] == "#123456"


@pytest.mark.django_db  # full_clean() перевіряє унікальність code через базу
@pytest.mark.parametrize("value", ["red", "#12345", "#1234567", "#12345g", "#fff;}"])
def test_invalid_color_is_rejected(value):
    config = ProviderConfig(code="upg", row_color=value)

    with pytest.raises(ValidationError):
        config.full_clean()


@pytest.mark.django_db
def test_row_colors_skip_empty_and_invalid():
    ProviderConfig.objects.create(code="upg", row_color="")
    # Обхід валідації (запис напряму в базу) не має потрапити в CSS сторінки.
    ProviderConfig.objects.create(code="petroline", row_color="x;}<b>")

    colors = ProviderConfig.row_colors()

    assert "upg" not in colors
    assert "petroline" not in colors
    assert colors["ukrnafta"] == DEFAULT_ROW_COLORS["ukrnafta"]


@pytest.fixture
def logged_client(client, django_user_model):
    user = django_user_model.objects.create_user(username="manager", password="x")
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_cards_page_shows_identifier_and_colors(logged_client):
    Card.objects.create(provider="petroline", number="ID-777", ext_number="1234 5678")
    Card.objects.create(provider="ukrnafta", number="0000111122223333")

    html = logged_client.get(reverse("cards")).content.decode()

    assert "<th>Ідентифікатор</th>" in html
    assert "<td>ID-777</td>" in html
    assert 'class="pv-petroline"' in html
    assert f"tr.pv-petroline > td {{ background: {DEFAULT_ROW_COLORS['petroline']}; }}" in html


@pytest.mark.django_db
def test_transactions_page_rows_have_provider_class(logged_client):
    Transaction.objects.create(
        provider="upg", external_id="TX-1", card_ref="C-1", timestamp=timezone.now()
    )

    html = logged_client.get(reverse("transactions")).content.decode()

    assert 'class="pv-upg"' in html
    assert f"tr.pv-upg > td {{ background: {DEFAULT_ROW_COLORS['upg']}; }}" in html
