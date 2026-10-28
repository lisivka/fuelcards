import csv
from urllib.parse import parse_qsl, urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from .models import CardStatus, Provider, Transaction
from .services import MAX_CATCHUP_DAYS, sync_since_last

# Параметри фільтрів, які дозволено повернути в адресу після синхронізації
_FILTER_KEYS = {"from", "to", "provider", "status", "card", "driver", "vehicle", "q", "page"}

STATUS_UNKNOWN = "unknown"
STATUS_CHOICES = [(str(value), label) for value, label in CardStatus.choices] + [(STATUS_UNKNOWN, "Невідомо")]

# Колонки, які користувач може ховати (ключ, підпис). «Дата» ховати не можна.
COLUMN_CHOICES = [
    ("provider", "Пост."),
    ("kind", "Тип"),
    ("card", "Картка"),
    ("vraw", "Авто (пост.)"),
    ("driver", "Водій"),
    ("vehicle", "Авто"),
    ("status", "Статус"),
    ("fuel", "Пальне"),
    ("liters", "Літри"),
    ("price", "Ціна"),
    ("amount", "Сума"),
    ("station", "АЗС"),
]


@login_required
def transactions(request):
    today = timezone.localdate()
    g = request.GET
    d_from = parse_date(g.get("from", "")) or today
    d_to = parse_date(g.get("to", "")) or today
    provider = g.get("provider", "")
    status = g.get("status", "")
    card = g.get("card", "").strip()
    driver = g.get("driver", "").strip()
    vehicle = g.get("vehicle", "").strip()
    q = g.get("q", "").strip()

    qs = Transaction.objects.select_related("card").filter(
        timestamp__date__gte=d_from, timestamp__date__lte=d_to
    )
    if provider:
        qs = qs.filter(provider=provider)
    if status == STATUS_UNKNOWN:
        qs = qs.filter(Q(card__isnull=True) | Q(card__status__isnull=True))
    elif status.isdigit() and int(status) in CardStatus.values:
        qs = qs.filter(card__status=int(status))
    if card:
        qs = qs.filter(card_ref__icontains=card)
    if driver:
        qs = qs.filter(card__driver_name__icontains=driver)
    if vehicle:
        qs = qs.filter(Q(card__vehicle__icontains=vehicle) | Q(vehicle_raw__icontains=vehicle))
    if q:
        qs = qs.filter(
            Q(card_ref__icontains=q) | Q(vehicle_raw__icontains=q)
            | Q(card__vehicle__icontains=q) | Q(card__driver_name__icontains=q)
            | Q(station__icontains=q)
        )

    if g.get("export") == "csv":
        return _export_csv(qs, d_from, d_to)

    totals = qs.filter(kind=Transaction.Kind.FUEL, canceled=False).aggregate(
        n=Count("id"), liters=Sum("liters"), amount=Sum("amount")
    )
    page = Paginator(qs, 100).get_page(g.get("page"))
    query = urlencode({
        "from": d_from.isoformat(), "to": d_to.isoformat(), "provider": provider, "status": status,
        "card": card, "driver": driver, "vehicle": vehicle, "q": q,
    })
    return render(request, "fuel/transactions.html", {
        "page": page, "totals": totals, "d_from": d_from, "d_to": d_to,
        "provider": provider, "status": status, "card": card, "driver": driver,
        "vehicle": vehicle, "q": q, "query": query,
        "providers": Provider.choices, "statuses": STATUS_CHOICES,
        "column_choices": COLUMN_CHOICES,
        "st_active": CardStatus.ACTIVE.value, "st_blocked": CardStatus.BLOCKED.value,
    })


@login_required
@require_POST
def transactions_sync(request):
    """Ручна синхронізація: від дня останньої транзакції кожного провайдера до сьогодні."""
    results, capped = sync_since_last()

    fetched = {name: res for name, res in results.items() if isinstance(res, tuple)}
    if fetched:
        total_new = sum(res[0] for res in fetched.values())
        if total_new:
            details = ", ".join(f"{Provider(name).label}: {res[0]}" for name, res in fetched.items())
            messages.success(request, f"Додано нових транзакцій: {total_new} ({details}).")
        else:
            messages.info(request, "Нових транзакцій немає.")

    for name, res in results.items():
        label = Provider(name).label
        if res is None:
            messages.info(request, f"{label}: пропущено (провайдер неактивний).")
        elif not isinstance(res, tuple):
            messages.error(request, f"{label}: помилка синхронізації, деталі в журналі.")

    for name in capped:
        messages.warning(
            request,
            f"{Provider(name).label}: розрив більший за {MAX_CATCHUP_DAYS} днів, завантажено лише "
            f"останні {MAX_CATCHUP_DAYS}. Для більшого періоду: manage.py sync_fuel --days N.",
        )

    params = [(k, v) for k, v in parse_qsl(request.POST.get("query", "")) if k in _FILTER_KEYS]
    url = reverse("transactions")
    if params:
        url += "?" + urlencode(params)
    return redirect(url)


def _export_csv(qs, d_from, d_to):
    """CSV завжди з повним набором колонок, незалежно від прихованих на екрані."""
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="fuel_{d_from}_{d_to}.csv"'
    resp.write("\ufeff")  # BOM для Excel
    w = csv.writer(resp, delimiter=";")
    w.writerow([
        "Дата", "Постачальник", "Тип", "Картка", "Авто (пост.)", "Водій", "Авто", "Статус",
        "Пальне", "Літри", "Ціна", "Сума", "АЗС", "Скасовано",
    ])
    for t in qs.order_by("timestamp"):
        card = t.card
        status = card.get_status_display() if card and card.status is not None else ""
        w.writerow([
            timezone.localtime(t.timestamp).strftime("%d.%m.%Y %H:%M:%S"), t.get_provider_display(),
            t.get_kind_display(), t.card_ref, t.vehicle_raw,
            card.driver_name if card else "", card.vehicle if card else "", status,
            t.fuel,
            t.liters if t.liters is not None else "", t.price if t.price is not None else "",
            t.amount if t.amount is not None else "", t.station, "так" if t.canceled else "",
        ])
    return resp