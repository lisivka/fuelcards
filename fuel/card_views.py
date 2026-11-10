import json
import logging

import requests
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from . import card_service as svc
from . import limits, managers
from .models import AuditLog, Card, CardStatus, Provider, ProviderConfig, VehicleType
from .providers.petroline_cards import PetrolineError
from .providers.ukrnafta_cards import UkrnaftaError

log = logging.getLogger(__name__)


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
    rows = [svc.serialize(c, editable) for c in Card.objects.select_related("vehicle_type", "manager")]
    options = _manager_options()
    return render(
        request,
        "fuel/cards.html",
        {
            "rows": rows,
            "vehicle_types": VehicleType.objects.all(),
            "manager_options": options,
            "manager_option_ids": {pk for pk, _ in options},
            "manager_filters": managers.filter_choices(),
            "me_id": request.user.pk,
            "providers": Provider.choices,
            "provider_colors": ProviderConfig.row_colors(),
            "duplicates_count": len(svc.person_duplicates()),
            "mismatch_count": sum(1 for r in rows if r["pl_mismatch"]),
            "limits_help": limits.HELP_TEXT,
            "sync_buttons": [
                {"provider": code, "label": label, "kind": "cards"}
                for code, label in svc.SYNC_LABELS.items()
                if syncable.get(code)
            ]
            + [
                {"provider": code, "label": label, "kind": "limits"}
                for code, label in svc.LIMIT_SYNC_LABELS.items()
                if syncable.get(code)
            ],
        },
    )


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
        vehicle_type = svc.parse_vehicle_type(data.get("vehicle_type"))
        manager = managers.parse_manager(data.get("manager"))
        with transaction.atomic():
            card = Card.objects.create(
                provider=provider,
                number=number,
                vehicle=str(data.get("vehicle", "")).strip()[:120],
                driver_name=" ".join(str(data.get("driver_name", "")).split())[:120],
                vehicle_type=vehicle_type,
                manager=manager,
            )
            changes = {
                f: [None, getattr(card, f)] for f in ("number", "vehicle", "driver_name") if getattr(card, f)
            }
            if vehicle_type:
                changes["vehicle_type"] = [None, vehicle_type.name]
            if manager:
                changes["manager"] = [None, managers.user_label(manager)]
            svc.record_audit(AuditLog.Action.CARD_CREATE, card, request.user, changes=changes)
        return {"id": card.pk}

    return _safe(run)


@login_required
@require_POST
def card_info(request, pk):
    card = get_object_or_404(Card, pk=pk)
    data = _body(request)

    def run():
        if card.provider == Provider.PETROLINE.value and svc.info_via_api(card.provider):
            # Тут немає попередження про спільного водія і полів «Модель / Держномер»
            raise ValueError("Авто/ПІБ картки Petroline змінюються на сторінці картки")
        svc.update_info(
            card, str(data.get("vehicle", "")), str(data.get("driver_name", "")), user=request.user
        )
        return {"card": _json_card(card)}

    return _safe(run)


@login_required
@require_POST
def card_vehicle_type(request, pk):
    """Тип авто зі списку «Карти»: лише в нашій базі, зберігається одразу після вибору."""
    card = get_object_or_404(Card.objects.select_related("vehicle_type"), pk=pk)
    data = _body(request)

    def run():
        changed = svc.set_vehicle_type(
            card, svc.parse_vehicle_type(data.get("vehicle_type")), user=request.user
        )
        return {"card": _json_card(card), "changed": changed}

    return _safe(run)


@login_required
@require_POST
def card_manager(request, pk):
    """Менеджер зі списку «Карти»: лише в нашій базі, зберігається одразу після вибору."""
    card = get_object_or_404(Card.objects.select_related("manager"), pk=pk)
    data = _body(request)

    def run():
        manager = managers.parse_manager(data.get("manager"), current_id=card.manager_id)
        changed = svc.set_manager(card, manager, user=request.user)
        return {"card": _json_card(card), "changed": changed}

    return _safe(run)


def _manager_options(card: Card | None = None) -> list[tuple[int, str]]:
    """Вибір менеджера: активні користувачі з групи «Менеджери» і поточний менеджер картки,
    навіть якщо його вже прибрали з групи (інакше збереження форми мовчки зняло б його)."""
    options = [(user.pk, managers.user_label(user)) for user in managers.assignable_managers()]
    if card is not None and card.manager_id and card.manager_id not in {pk for pk, _ in options}:
        options.append((card.manager_id, f"{card.manager_name} (не менеджер)"))
    return options


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
    """Оновлення карток із API провайдера; без параметра це UkrNafta (як раніше).
    kind="limits" — окремо ліміти активних карток (довго: запит на кожну картку).
    """
    data = _body(request)
    provider = data.get("provider") or Provider.UKRNAFTA.value

    def run():
        if data.get("kind") == "limits":
            done, failed = svc.sync_limits(provider, user=request.user)
            message = f"оновлено {done}" + (f", з помилкою {failed} (див. лог сервера)" if failed else "")
            return {"updated": done, "failed": failed, "message": message}
        created, updated, warning = svc.sync_cards(provider, user=request.user)
        message = f"нових {created}, оновлено {updated}" + (f"; {warning}" if warning else "")
        return {"created": created, "updated": updated, "failed": bool(warning), "message": message}

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
            "vehicle_types": VehicleType.objects.all(),
            "manager_options": _manager_options(card),
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
            "limits_help": limits.HELP_TEXT,
            "limits_lines": limits.describe(card.fuel_limits),
            "limits_note": svc.limits_note(card),
            "can_refresh_limits": card.provider in svc.LIMIT_PROVIDERS and svc.sync_map().get(card.provider),
        },
    )


@login_required
def card_duplicates(request):
    """Водії Petroline з кількома активними картками: зміна Авто/ПІБ однієї зачепить усі."""
    return render(
        request,
        "fuel/card_duplicates.html",
        {"groups": svc.person_duplicates(), "st_active": CardStatus.ACTIVE.value},
    )


def _card_detail_post(request, card: Card) -> None:
    """Дії форми сторінки картки; результат показується повідомленням після редиректу."""
    action = request.POST.get("action")
    try:
        if action in ("save", "push"):
            # Тип авто перевіряємо до запису в API, а зберігаємо після: невдача API не лишає половину змін
            vehicle_type = (
                svc.parse_vehicle_type(request.POST["vehicle_type"])
                if "vehicle_type" in request.POST
                else card.vehicle_type
            )
            manager = (
                managers.parse_manager(request.POST["manager"], current_id=card.manager_id)
                if "manager" in request.POST
                else card.manager
            )
            # Поля, яких форма не надсилає (вимкнені або окрема кнопка звірки), лишаються як є
            changed = svc.update_info(
                card,
                request.POST.get("vehicle", card.vehicle),
                request.POST.get("driver_name", card.driver_name),
                request.POST.get("note"),
                user=request.user,
                push=action == "push",
            )
            changed = svc.set_vehicle_type(card, vehicle_type, user=request.user) or changed
            changed = svc.set_manager(card, manager, user=request.user) or changed
            if changed:
                messages.success(request, "Зміни збережено.")
            else:
                messages.info(request, "Змін немає.")
        elif action == "take":
            if svc.take_from_petroline(card, user=request.user):
                messages.success(request, "Авто/ПІБ взято з Petroline (змінено лише в нашій базі).")
            else:
                messages.info(request, "Змін немає: наші дані вже як у Petroline.")
        elif action == "limits":
            svc.refresh_limits(card, strict=True)
            messages.success(request, f"Ліміти оновлено: {card.limits_short or 'лімітів на пальне немає'}.")
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
    except Exception:
        # Несподіваний формат відповіді провайдера тощо — повідомлення замість сторінки 500
        log.exception("Сторінка картки %s: дія %s", card.pk, action)
        messages.error(request, "Несподівана помилка, зміни не збережено. Подробиці — в лозі сервера.")
