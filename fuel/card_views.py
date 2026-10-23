import json

import requests
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_POST

from . import card_service as svc
from .models import Card, Provider
from .providers.ukrnafta_cards import UkrnaftaError


def _body(request) -> dict:
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return {}


def _safe(fn):
    """Виконує дію й перетворює помилки на JSON із повідомленням."""
    try:
        return JsonResponse(fn())
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    except (UkrnaftaError, requests.RequestException) as exc:
        return JsonResponse({"error": f"Помилка провайдера: {exc}"}, status=502)


@login_required
def cards(request):
    editable = svc.editable_map()
    rows = [svc.serialize(c, editable) for c in Card.objects.all()]
    return render(request, "fuel/cards.html", {
        "rows": rows,
        "providers": Provider.choices,
        "sync_available": editable.get(Provider.UKRNAFTA.value, False),
    })


@login_required
@require_POST
def card_create(request):
    data = _body(request)

    def run():
        provider = data.get("provider", "")
        number = str(data.get("number", "")).strip()
        if provider not in Provider.values:
            raise ValueError("Невідомий провайдер")
        if not number:
            raise ValueError("Вкажіть номер картки")
        if Card.objects.filter(provider=provider, number=number).exists():
            raise ValueError("Така картка вже є")
        card = Card.objects.create(
            provider=provider, number=number,
            vehicle=str(data.get("vehicle", "")).strip()[:120],
            driver_name=" ".join(str(data.get("driver_name", "")).split())[:120],
        )
        return {"id": card.pk}

    return _safe(run)


@login_required
@require_POST
def card_info(request, pk):
    card = get_object_or_404(Card, pk=pk)
    data = _body(request)

    def run():
        svc.update_info(card, str(data.get("vehicle", "")), str(data.get("driver_name", "")))
        return {"card": _json_card(card)}

    return _safe(run)


@login_required
@require_POST
def card_status(request, pk):
    card = get_object_or_404(Card, pk=pk)
    data = _body(request)

    def run():
        try:
            new_status = int(data.get("status"))
        except (TypeError, ValueError):
            raise ValueError("Некоректний статус")
        svc.set_status(card, new_status)
        return {"card": _json_card(card)}

    return _safe(run)


@login_required
@require_POST
def card_delete(request, pk):
    get_object_or_404(Card, pk=pk).delete()  # лише локально
    return JsonResponse({"ok": True})


@login_required
@require_POST
def cards_sync(request):
    def run():
        if not svc.editable_map().get(Provider.UKRNAFTA.value):
            raise ValueError("UkrNafta неактивний або режим редагування вимкнений")
        created, updated = svc.sync_ukrnafta_cards()
        return {"created": created, "updated": updated}

    return _safe(run)


def _json_card(card):
    row = svc.serialize(card)
    row["status_updated"] = row["status_updated"].isoformat() if row["status_updated"] else None
    return row
