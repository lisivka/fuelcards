import csv
from datetime import date, timedelta
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

from .models import AuditLog, CardStatus, Provider, ProviderConfig, Transaction, VehicleType
from .services import MAX_CATCHUP_DAYS, sync_since_last

# Параметри фільтрів, які дозволено повернути в адресу після синхронізації
_FILTER_KEYS = {
    "from", "to", "period", "provider", "status", "vtype", "card", "driver", "vehicle", "q", "page", "per_page"
}

STATUS_UNKNOWN = "unknown"
STATUS_CHOICES = [(str(value), label) for value, label in CardStatus.choices] + [(STATUS_UNKNOWN, "Невідомо")]

# Колонки, які користувач може ховати (ключ, підпис). «Дата» ховати не можна.
COLUMN_CHOICES = [
    ("provider", "Пост."),
    ("kind", "Тип"),
    ("card", "Картка"),
    ("cardid", "Ідентифікатор"),
    ("vraw", "Авто (пост.)"),
    ("driver", "Водій"),
    ("vehicle", "Авто"),
    ("vtype", "Тип авто"),
    ("status", "Статус"),
    ("fuel", "Пальне"),
    ("liters", "Літри"),
    ("price", "Ціна"),
    ("amount", "Сума"),
    ("station", "АЗС"),
]

# Швидкі періоди: (код у адресі, підпис). Період відносний — збережений звіт «7 днів» завтра покаже нові 7 днів.
PERIOD_PRESETS = [
    ("today", "Сьогодні"),
    ("yesterday", "Вчора"),
    ("7d", "7 днів"),
    ("30d", "30 днів"),
]
# Фільтр «Тип авто»: "" — усі, VTYPE_NONE — картки без типу (і транзакції без картки), інакше id типу
VTYPE_NONE = "none"

PER_PAGE_CHOICES = [25, 50, 100, 200, 500]
DEFAULT_PER_PAGE = 100


def preset_dates(code: str, today: date) -> tuple[date, date] | None:
    """Дати швидкого періоду (обидві межі включно) або None для невідомого коду."""
    if code == "today":
        return today, today
    if code == "yesterday":
        yesterday = today - timedelta(days=1)
        return yesterday, yesterday
    if code == "7d":
        return today - timedelta(days=6), today
    if code == "30d":
        return today - timedelta(days=29), today
    return None


def parse_vtype(value: str | None) -> str:
    """Значення фільтра «Тип авто»: "", VTYPE_NONE або id наявного типу; решта — як «усі»."""
    value = (value or "").strip()
    if value == VTYPE_NONE:
        return value
    if value.isdigit() and VehicleType.objects.filter(pk=int(value)).exists():
        return value
    return ""


def vtype_q(vtype: str, card_field: str = "card") -> Q:
    """Умова фільтра «Тип авто» для моделі з посиланням на картку (`card_field`)."""
    if vtype == VTYPE_NONE:
        return Q(**{f"{card_field}__vehicle_type__isnull": True})
    if vtype:
        return Q(**{f"{card_field}__vehicle_type_id": int(vtype)})
    return Q()


def vtype_choices() -> list[tuple[str, str]]:
    types = VehicleType.objects.values_list("pk", "name")
    return [(VTYPE_NONE, "Без типу")] + [(str(pk), name) for pk, name in types]


def parse_per_page(value: str | None) -> int:
    """Кількість рядків на сторінці; лише значення зі списку, інакше типове."""
    try:
        number = int(value or "")
    except ValueError:
        return DEFAULT_PER_PAGE
    return number if number in PER_PAGE_CHOICES else DEFAULT_PER_PAGE


@login_required
def transactions(request):
    today = timezone.localdate()
    g = request.GET
    period = g.get("period", "")
    dates = preset_dates(period, today)
    if dates:
        d_from, d_to = dates
    else:
        period = ""
        d_from = parse_date(g.get("from", "")) or today
        d_to = parse_date(g.get("to", "")) or today
    per_page = parse_per_page(g.get("per_page"))
    provider = g.get("provider", "")
    status = g.get("status", "")
    vtype = parse_vtype(g.get("vtype"))
    card = g.get("card", "").strip()
    driver = g.get("driver", "").strip()
    vehicle = g.get("vehicle", "").strip()
    q = g.get("q", "").strip()

    qs = Transaction.objects.select_related("card", "card__vehicle_type").filter(
        timestamp__date__gte=d_from, timestamp__date__lte=d_to
    )
    qs = qs.filter(vtype_q(vtype))
    if provider:
        qs = qs.filter(provider=provider)
    if status == STATUS_UNKNOWN:
        qs = qs.filter(Q(card__isnull=True) | Q(card__status__isnull=True))
    elif status.isdigit() and int(status) in CardStatus.values:
        qs = qs.filter(card__status=int(status))
    if card:
        qs = qs.filter(Q(card_ref__icontains=card) | Q(card__ext_number__icontains=card))
    if driver:
        qs = qs.filter(card__driver_name__icontains=driver)
    if vehicle:
        qs = qs.filter(Q(card__vehicle__icontains=vehicle) | Q(vehicle_raw__icontains=vehicle))
    if q:
        qs = qs.filter(
            Q(card_ref__icontains=q) | Q(card__ext_number__icontains=q) | Q(vehicle_raw__icontains=q)
            | Q(card__vehicle__icontains=q) | Q(card__driver_name__icontains=q)
            | Q(station__icontains=q)
        )

    if g.get("export") == "csv":
        return _export_csv(qs, d_from, d_to)

    totals = qs.filter(kind=Transaction.Kind.FUEL, canceled=False).aggregate(
        n=Count("id"), liters=Sum("liters"), amount=Sum("amount")
    )
    paginator = Paginator(qs, per_page)
    page = paginator.get_page(g.get("page"))
    page_range = list(paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=3))
    period_params = {"period": period} if period else {"from": d_from.isoformat(), "to": d_to.isoformat()}
    filter_params = {
        "provider": provider, "status": status, "vtype": vtype, "card": card, "driver": driver,
        "vehicle": vehicle, "q": q,
    }
    query = urlencode(
        {**period_params, **{k: v for k, v in filter_params.items() if v}, "per_page": per_page}
    )
    return render(request, "fuel/transactions.html", {
        "page": page, "page_range": page_range, "ellipsis": paginator.ELLIPSIS,
        "totals": totals, "d_from": d_from, "d_to": d_to, "period": period,
        "period_presets": PERIOD_PRESETS, "per_page": per_page, "per_page_choices": PER_PAGE_CHOICES,
        "provider": provider, "status": status, "vtype": vtype, "card": card, "driver": driver,
        "vehicle": vehicle, "q": q, "query": query,
        "providers": Provider.choices, "statuses": STATUS_CHOICES, "vtypes": vtype_choices(),
        "column_choices": COLUMN_CHOICES,
        "provider_colors": ProviderConfig.row_colors(),
        "st_active": CardStatus.ACTIVE.value, "st_blocked": CardStatus.BLOCKED.value,
    })


@login_required
@require_POST
def transactions_sync(request):
    """Ручна синхронізація: від дня останньої транзакції кожного провайдера до сьогодні."""
    results, capped = sync_since_last(user=request.user)

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
        "Дата", "Постачальник", "Тип", "Картка", "Ідентифікатор", "Авто (пост.)", "Водій", "Авто",
        "Тип авто", "Статус", "Пальне", "Літри", "Ціна", "Сума", "АЗС", "Скасовано",
    ])
    for t in qs.order_by("timestamp"):
        card = t.card
        status = card.get_status_display() if card and card.status is not None else ""
        w.writerow([
            timezone.localtime(t.timestamp).strftime("%d.%m.%Y %H:%M:%S"), t.get_provider_display(),
            t.get_kind_display(), card.display_number if card else t.card_ref, t.card_ref, t.vehicle_raw,
            card.driver_name if card else "", card.vehicle if card else "",
            card.vehicle_type.name if card and card.vehicle_type_id else "", status,
            t.fuel,
            t.liters if t.liters is not None else "", t.price if t.price is not None else "",
            t.amount if t.amount is not None else "", t.station, "так" if t.canceled else "",
        ])
    return resp

# Сторінка «Логи»: фільтр «Дія» -> набір дій журналу
LOG_KINDS = [
    ("", "Усі"),
    ("cards", "Зміни карток"),
    ("sync", "Синхронізації"),
]
LOGS_PER_PAGE = 20


@login_required
def logs(request):
    """Журнал аудиту для всіх авторизованих: зміни карток і синхронізації."""
    g = request.GET
    kind = g.get("kind", "")
    provider = g.get("provider", "")
    errors_only = g.get("errors") == "1"
    vtype = parse_vtype(g.get("vtype"))

    qs = AuditLog.objects.select_related("card")
    if vtype:
        # Записи без картки (синхронізації, видалені картки) не мають типу авто й під фільтр не потрапляють
        qs = qs.filter(card__isnull=False).filter(vtype_q(vtype))
    if kind == "cards":
        qs = qs.filter(action__in=AuditLog.CARD_ACTIONS)
    elif kind == "sync":
        qs = qs.filter(action__in=AuditLog.SYNC_ACTIONS)
    else:
        kind = ""
    if provider in Provider.values:
        qs = qs.filter(provider=provider)
    else:
        provider = ""
    if errors_only:
        qs = qs.filter(success=False)

    paginator = Paginator(qs, LOGS_PER_PAGE)
    page = paginator.get_page(g.get("page"))
    params = {"kind": kind, "provider": provider, "vtype": vtype, "errors": "1" if errors_only else ""}
    return render(
        request,
        "fuel/logs.html",
        {
            "page": page,
            "page_range": list(paginator.get_elided_page_range(page.number, on_each_side=2, on_ends=3)),
            "ellipsis": paginator.ELLIPSIS,
            # kind= завжди в адресі: так скрипт збереження бачить, що фільтри задано явно
            "query": urlencode({"kind": kind, **{k: v for k, v in params.items() if v and k != "kind"}}),
            "kind": kind,
            "provider": provider,
            "vtype": vtype,
            "vtypes": vtype_choices(),
            "errors_only": errors_only,
            "kinds": LOG_KINDS,
            "providers": Provider.choices,
        },
    )
