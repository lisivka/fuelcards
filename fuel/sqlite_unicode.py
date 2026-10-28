"""Unicode-сумісний LIKE для SQLite.

Стандартний LIKE у SQLite ігнорує регістр лише для ASCII, тож «іваненко» не знаходило «Іваненко».
Підміняємо SQL-функцію like() на реалізацію, що порівнює без урахування регістру для будь-яких літер.
Це зачіпає всі lookup-и Django (icontains, contains, startswith, ...) та пошук в адмінці.
"""
import re
from functools import lru_cache

from django.db.backends.signals import connection_created


@lru_cache(maxsize=512)
def _compile(pattern: str, escape: str | None) -> re.Pattern:
    """Перетворює SQL-шаблон LIKE (% і _ та символ ESCAPE) на регулярний вираз."""
    parts = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if escape and ch == escape and i + 1 < len(pattern):
            i += 1
            parts.append(re.escape(pattern[i]))
        elif ch == "%":
            parts.append(".*")
        elif ch == "_":
            parts.append(".")
        else:
            parts.append(re.escape(ch))
        i += 1
    return re.compile("".join(parts), re.IGNORECASE | re.DOTALL)


def unicode_like(pattern, value, escape=None):
    """SQL: value LIKE pattern [ESCAPE escape]. SQLite передає аргументи як (pattern, value[, escape])."""
    if pattern is None or value is None:
        return None
    return 1 if _compile(str(pattern), escape or None).fullmatch(str(value)) else 0


def _register(sender, connection, **kwargs):
    if connection.vendor != "sqlite":
        return
    raw = connection.connection
    raw.create_function("like", 2, unicode_like, deterministic=True)
    raw.create_function("like", 3, unicode_like, deterministic=True)


def install():
    connection_created.connect(_register, dispatch_uid="fuel_sqlite_unicode_like")