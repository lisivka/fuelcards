import json

import requests
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from . import card_service as svc
from .models import AuditLog, Card, CardStatus, Provider, ProviderConfig
from .providers.petroline_cards import PetrolineError
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
    except (UkrnaftaError, PetrolineError, requests.RequestException) as exc:
        return JsonResponse({"error": f"Помилка провайдера: {exc}"}, status=502)


@login_required
def cards(request):
    editable = svc.editable_map()
    syncable = svc.sync_map()
    rows = [svc.serialize(c, editable) for c in Card.objects.all()]
    return render(request, "fuel/cards.html", {
        "rows": rows,
        "providers": Provider.choices,
        "provider_colors": ProviderConfig.row_colors(),
        "sync_buttons": [
            {"provider": code, "label": label}
            for code, label in svc.SYNC_LABELS.items() if syncable.get(code)
        ],
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
        with transaction.atomic():
            card = Card.objects.create(
                provider=provider,
                number=number,
                vehicle=str(data.get("vehicle", "")).strip()[:120],
                driver_name=" ".join(str(data.get("driver_name", "")).split())[:120],
            )
            changes = {f: [None, getattr(card, f)] for f in ("number", "vehicle", "driver_name") if getattr(card, f)}
            svc.record_audit(AuditLog.Action.CARD_CREATE, card, request.user, changes=changes)
        return {"id": card.pk}

    return _safe(run)


@login_required
@require_POST
def card_info(request, pk):
    card = get_object_or_404(Card, pk=pk)
    data = _body(request)

    def run():
        svc.update_info(card, str(data.get("vehicle", "")), str(data.get("driver_name", "")), user=request.user)
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
            raise ValueError("Некоректний статус") from None
        svc.set_status(card, new_status, user=request.user)
        return {"card": _json_card(card)}

    return _safe(run)


@login_required
@require_POST
def card_delete(request, pk):
    card = get_object_or_404(Card, pk=pk)
    with transaction.atomic():  # лише локально, у провайдера картка лишається
        svc.record_audit(AuditLog.Action.CARD_DELETE, card, request.user)
        card.delete()
    return JsonResponse({"ok": True})


@login_required
@require_POST
def cards_sync(request):
    """Оновлення карток із API провайдера; без параметра це UkrNafta (як раніше)."""
    provider = _body(request).get("provider") or Provider.UKRNAFTA.value

    def run():
        created, updated = svc.sync_cards(provider, user=request.user)
        return {"created": created, "updated": updated}

    return _safe(run)


def _json_card(card):
    row = svc.serialize(card)
    row["status_updated"] = row["status_updated"].isoformat() if row["status_updated"] else None
    return row

# Куди повертає кнопка «Назад» зі сторінки картки (лише зі списку — без відкритих редиректів)
# Ключі — імена URL-маршрутів
BACK_TARGETS = {"transactions": "Транзакції", "cards": "Карти", "logs": "Логи"}
CARD_PAGE_TRANSACTIONS = 20
CARD_PAGE_AUDIT = 20


@login_required
@require_http_methods(["GET", "POST"])
def card_detail(request, pk):
    """Сторінка картки: перегляд, редагування полів (залежно від провайдера), блокування."""
    card = get_object_or_404(Card, pk=pk)
    back = request.POST.get("back") or request.GET.get("back") or ""
    if back not in BACK_TARGETS:
        back = "transactions"

    if request.method == "POST":
        _card_detail_post(request, card)
        return redirect(f"{reverse('card_detail', args=[card.pk])}?back={back}")

    transactions = list(card.transactions.order_by("-timestamp")[:CARD_PAGE_TRANSACTIONS])
    oldest = card.transactions.order_by("timestamp").values_list("timestamp", flat=True).first()
    return render(
        request,
        "fuel/card_detail.html",
        {
            "card": card,
            "caps": svc.capabilities(card),
            "provider_colors": ProviderConfig.row_colors(),
            "transactions": transactions,
            "transactions_total": card.transactions.count(),
            "oldest_tx": oldest,
            "audit_entries": card.audit_entries.select_related("user")[:CARD_PAGE_AUDIT],
            "back": back,
            "back_label": BACK_TARGETS[back],
            "back_url": reverse(back),
            "st_active": CardStatus.ACTIVE.value,
            "st_blocked": CardStatus.BLOCKED.value,
        },
    )


def _card_detail_post(request, card: Card) -> None:
    """Дії форми сторінки картки; результат показується повідомленням після редиректу."""
    action = request.POST.get("action")
    try:
        if action == "save":
            changed = svc.update_info(
                card,
                request.POST.get("vehicle", ""),
                request.POST.get("driver_name", ""),
                request.POST.get("note", ""),
                user=request.user,
            )
            if changed:
                messages.success(request, "Зміни збережено.")
            else:
                messages.info(request, "Змін немає.")
        elif action == "status":
            try:
                new_status = int(request.POST.get("status", ""))
            except ValueError:
                raise ValueError("Некоректний статус") from None
            svc.set_status(card, new_status, user=request.user)
            word = "розблоковано" if new_status == CardStatus.ACTIVE else "заблоковано"
            messages.success(request, f"Картку {word}.")
        else:
            messages.error(request, "Невідома дія.")
    except ValueError as exc:
        messages.error(request, str(exc))
    except (UkrnaftaError, PetrolineError, requests.RequestException) as exc:
        messages.error(request, f"Помилка провайдера, зміни не збережено: {exc}")
