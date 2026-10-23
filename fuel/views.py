import csv
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.dateparse import parse_date

from .models import Provider, Transaction


@login_required
def transactions(request):
    today = timezone.localdate()
    d_from = parse_date(request.GET.get("from", "")) or today
    d_to = parse_date(request.GET.get("to", "")) or today
    provider = request.GET.get("provider", "")
    q = request.GET.get("q", "").strip()

    qs = Transaction.objects.select_related("card").filter(
        timestamp__date__gte=d_from, timestamp__date__lte=d_to
    )
    if provider:
        qs = qs.filter(provider=provider)
    if q:
        qs = qs.filter(
            Q(card_ref__icontains=q) | Q(vehicle_raw__icontains=q)
            | Q(card__vehicle__icontains=q) | Q(station__icontains=q)
        )

    if request.GET.get("export") == "csv":
        return _export_csv(qs, d_from, d_to)

    totals = qs.filter(kind=Transaction.Kind.FUEL, canceled=False).aggregate(
        n=Count("id"), liters=Sum("liters"), amount=Sum("amount")
    )
    page = Paginator(qs, 100).get_page(request.GET.get("page"))
    query = urlencode({"from": d_from.isoformat(), "to": d_to.isoformat(), "provider": provider, "q": q})
    return render(request, "fuel/transactions.html", {
        "page": page, "totals": totals, "d_from": d_from, "d_to": d_to,
        "provider": provider, "q": q, "providers": Provider.choices, "query": query,
    })


def _export_csv(qs, d_from, d_to):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="fuel_{d_from}_{d_to}.csv"'
    resp.write("\ufeff")  # BOM для Excel
    w = csv.writer(resp, delimiter=";")
    w.writerow(["Дата", "Постачальник", "Тип", "Картка", "Авто", "Пальне", "Літри", "Ціна", "Сума", "АЗС", "Скасовано"])
    for t in qs.order_by("timestamp"):
        w.writerow([
            timezone.localtime(t.timestamp).strftime("%d.%m.%Y %H:%M:%S"), t.get_provider_display(),
            t.get_kind_display(), t.card_ref, t.vehicle_display, t.fuel,
            t.liters if t.liters is not None else "", t.price if t.price is not None else "",
            t.amount if t.amount is not None else "", t.station, "так" if t.canceled else "",
        ])
    return resp
