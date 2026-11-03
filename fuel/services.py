import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Max
from django.utils import timezone

from .audit import record_sync
from .models import MAX_SYNC_DAYS, AuditLog, Card, ProviderConfig, Transaction, sync_period_error
from .providers import PROVIDERS
from .providers.base import NormalizedTx

log = logging.getLogger(__name__)

DEFAULT_DAYS = 2  # якщо транзакцій провайдера в базі ще немає
MAX_CATCHUP_DAYS = 31  # межа «догонки» для кнопки; довший період: sync_fuel --days N
CHUNK_DAYS = 30  # довгий період ріжемо на частини, щоб не робити до API один великий запит
CHUNK_PAUSE_SECONDS = 2.0  # пауза між запитами частин

MODE_DAYS = "days"  # глибина з ProviderConfig.sync_days
MODE_PERIOD = "period"  # дати з ProviderConfig.sync_date_from / sync_date_to


@dataclass(frozen=True)
class SyncMessage:
    """Повідомлення про хід синхронізації для адмінки й команди."""

    level: str  # "info" | "success" | "warning" | "error"
    text: str


def save_transactions(items: list[NormalizedTx]) -> tuple[int, int]:
    created = updated = 0
    with transaction.atomic():
        for it in items:
            card = None
            if it.card_ref:
                card, _ = Card.objects.get_or_create(
                    provider=it.provider, number=it.card_ref, defaults={"vehicle": it.vehicle[:120]}
                )
            _, was_created = Transaction.objects.update_or_create(
                provider=it.provider,
                external_id=it.external_id,
                defaults={
                    "kind": it.kind,
                    "card": card,
                    "card_ref": it.card_ref,
                    "vehicle_raw": it.vehicle[:200],
                    "timestamp": it.timestamp,
                    "fuel": it.fuel,
                    "liters": it.liters,
                    "price": it.price,
                    "amount": it.amount,
                    "station": it.station,
                    "canceled": it.canceled,
                    "raw": it.raw,
                },
            )
            created += was_created
            updated += not was_created
    return created, updated


def run_sync(provider_names: list[str], date_from: date, date_to: date) -> dict:
    """Повертає {provider: (created, updated) | None | 'error: ...'}.

    None означає, що провайдера пропущено, бо він неактивний.
    Збій одного провайдера не зупиняє інших.
    """
    configs = ProviderConfig.ensure_all()
    results = {}
    for name in provider_names:
        config = configs.get(name)
        if config is not None and not config.is_active:
            log.info("Провайдер %s неактивний, пропускаємо", name)
            results[name] = None
            continue
        log.info("Синхронізація %s: %s .. %s", name, date_from, date_to)
        try:
            items = PROVIDERS[name]().fetch(date_from, date_to)
            results[name] = save_transactions(items)
            log.info("Синхронізація %s: нових %s, оновлено %s", name, *results[name])
        except Exception as exc:  # noqa: BLE001
            log.exception("Sync failed for %s", name)
            results[name] = f"error: {exc}"
    return results


def sync_since_last(*, user=None) -> tuple[dict, list[str]]:
    """Догружає кожному провайдеру дані від дня його останньої транзакції до сьогодні.

    Повертає (результати run_sync по провайдерах, провайдери, чий старт обмежено MAX_CATCHUP_DAYS).
    Кожен виконаний (не пропущений) провайдер пишеться в журнал аудиту.
    """
    today = timezone.localdate()
    earliest = today - timedelta(days=MAX_CATCHUP_DAYS)
    results: dict = {}
    capped: list[str] = []
    for name in PROVIDERS:
        last = Transaction.objects.filter(provider=name).aggregate(last=Max("timestamp"))["last"]
        if last is None:
            date_from = today - timedelta(days=DEFAULT_DAYS)
        else:
            date_from = timezone.localtime(last).date()
        was_capped = date_from < earliest
        if was_capped:
            date_from = earliest
        result = results[name] = run_sync([name], date_from, today)[name]
        if result is None:
            continue
        if was_capped and isinstance(result, tuple):
            capped.append(name)
        _record_tx_sync(
            name,
            result,
            user=user,
            source=AuditLog.SOURCE_BUTTON,
            date_from=date_from,
            date_to=today,
            capped=was_capped,
        )
    return results, capped


def _record_tx_sync(
    name: str, result, *, user, source: str, date_from: date, date_to: date, **details
) -> None:
    """Запис результату run_sync у журнал: (нових, оновлено) або текст помилки."""
    if isinstance(result, tuple):
        details.update(created=result[0], updated=result[1])
        error = ""
    else:
        error = str(result)
    record_sync(
        AuditLog.Action.TX_SYNC,
        name,
        user=user,
        source=source,
        date_from=date_from,
        date_to=date_to,
        error=error,
        **details,
    )


def split_period(date_from: date, date_to: date, chunk_days: int = CHUNK_DAYS) -> list[tuple[date, date]]:
    """Ділить період (обидві межі включно) на частини не довші за chunk_days днів."""
    chunks = []
    start = date_from
    while start <= date_to:
        end = min(start + timedelta(days=chunk_days - 1), date_to)
        chunks.append((start, end))
        start = end + timedelta(days=1)
    return chunks


def period_for_config(config: ProviderConfig, mode: str, today: date) -> tuple[date, date]:
    """Період синхронізації провайдера за його конфігурацією. ValueError — якщо період некоректний."""
    if mode == MODE_DAYS:
        days = config.sync_days
        if not days or days < 1 or days > MAX_SYNC_DAYS:
            raise ValueError(f"глибина {days} днів поза межами 1..{MAX_SYNC_DAYS}")
        return today - timedelta(days=days), today
    if mode == MODE_PERIOD:
        if config.sync_date_from is None or config.sync_date_to is None:
            raise ValueError("не задано період «з/по» у конфігурації")
        error = sync_period_error(config.sync_date_from, config.sync_date_to)
        if error:
            raise ValueError(error)
        return config.sync_date_from, config.sync_date_to
    raise ValueError(f"невідомий режим синхронізації: {mode}")


def build_periods(
    configs: Iterable[ProviderConfig], mode: str, today: date | None = None
) -> tuple[dict[str, tuple[date, date]], list[SyncMessage]]:
    """Періоди для вибраних конфігурацій; неактивні й некоректні пропускаються з повідомленням."""
    today = today or timezone.localdate()
    periods: dict[str, tuple[date, date]] = {}
    messages: list[SyncMessage] = []
    for config in configs:
        if config.code not in PROVIDERS:
            messages.append(SyncMessage("error", f"{config.code}: невідомий провайдер, пропущено"))
            continue
        if not config.is_active:
            messages.append(SyncMessage("warning", f"{config.code}: пропущено (провайдер неактивний)"))
            continue
        try:
            periods[config.code] = period_for_config(config, mode, today)
        except ValueError as exc:
            messages.append(SyncMessage("error", f"{config.code}: {exc}; пропущено"))
    return periods, messages


def sync_periods_chunked(
    periods: dict[str, tuple[date, date]],
    *,
    chunk_days: int = CHUNK_DAYS,
    pause: float = CHUNK_PAUSE_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    user=None,
    source: str = AuditLog.SOURCE_COMMAND,
) -> list[SyncMessage]:
    """Синхронізує кожного провайдера за його період частинами з паузою між запитами.

    Помилка в частині зупиняє решту частин цього провайдера (найчастіше це авторизація чи
    недоступність API), але не інших провайдерів. Підсумок по кожному провайдеру — у журнал аудиту.
    """
    messages: list[SyncMessage] = []
    first_request = True
    for name, (date_from, date_to) in periods.items():
        chunks = split_period(date_from, date_to, chunk_days)
        period_text = f"{date_from:%d.%m.%Y} .. {date_to:%d.%m.%Y}"
        if len(chunks) > 1:
            messages.append(
                SyncMessage(
                    "info",
                    f"{name}: період {period_text} розбито на {len(chunks)} частин по ≤{chunk_days} днів",
                )
            )
        created = updated = chunks_done = 0
        failed = skipped = False
        error = ""
        for index, (chunk_from, chunk_to) in enumerate(chunks, start=1):
            if not first_request:
                sleep(pause)
            first_request = False
            chunk_text = f"{chunk_from:%d.%m.%Y} .. {chunk_to:%d.%m.%Y}"
            prefix = f"{name}: частина {index}/{len(chunks)} ({chunk_text})" if len(chunks) > 1 else name
            result = run_sync([name], chunk_from, chunk_to)[name]
            if result is None:
                messages.append(SyncMessage("warning", f"{name}: пропущено (провайдер неактивний)"))
                skipped = True
                break
            if not isinstance(result, tuple):
                rest = "; решту частин пропущено" if index < len(chunks) else ""
                messages.append(SyncMessage("error", f"{prefix}: {result}{rest}"))
                failed = True
                error = str(result)
                break
            created += result[0]
            updated += result[1]
            chunks_done += 1
            if len(chunks) > 1:
                messages.append(SyncMessage("info", f"{prefix}: нових {result[0]}, оновлено {result[1]}"))
        if skipped:
            continue
        record_sync(
            AuditLog.Action.TX_SYNC,
            name,
            user=user,
            source=source,
            date_from=date_from,
            date_to=date_to,
            error=error,
            chunks=len(chunks),
            chunks_done=chunks_done,
            created=created,
            updated=updated,
        )
        if failed:
            if created or updated:
                messages.append(
                    SyncMessage(
                        "warning", f"{name}: до помилки збережено нових {created}, оновлено {updated}"
                    )
                )
            continue
        messages.append(
            SyncMessage("success", f"{name} ({period_text}): нових {created}, оновлено {updated}")
        )
    return messages


def sync_configs(configs: Iterable[ProviderConfig], mode: str, **kwargs) -> list[SyncMessage]:
    """Синхронізація вибраних провайдерів за періодами з їхніх конфігурацій (адмінка, sync_fuel).

    kwargs ідуть у sync_periods_chunked (user, source, pause тощо).
    """
    periods, messages = build_periods(configs, mode)
    if not periods:
        messages.append(SyncMessage("warning", "Немає провайдерів для синхронізації"))
        return messages
    return messages + sync_periods_chunked(periods, **kwargs)
