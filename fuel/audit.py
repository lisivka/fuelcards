"""Запис службових подій (синхронізацій) у журнал аудиту."""

import logging
from datetime import date

from .models import AuditLog

log = logging.getLogger(__name__)


def username_of(user) -> str:
    return user.get_username() if user is not None and getattr(user, "is_authenticated", False) else ""


def record_sync(
    action: str,
    provider: str,
    *,
    user=None,
    source: str,
    date_from: date | None = None,
    date_to: date | None = None,
    error: str = "",
    **details,
) -> AuditLog:
    """Один запис на провайдера за запуск синхронізації (транзакцій або карток)."""
    payload = {"source": source, **details}
    if date_from is not None and date_to is not None:
        payload.update(date_from=date_from.isoformat(), date_to=date_to.isoformat())
    username = username_of(user)
    entry = AuditLog.objects.create(
        user=user if username else None,
        username=username,
        action=action,
        provider=provider,
        details=payload,
        via_api=True,
        success=not error,
        error=error[:1000],
    )
    log.info("Аудит: %s %s %s успіх=%s %s %s", username or "-", action, provider, not error, payload, error)
    return entry
