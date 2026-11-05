"""Спільне для тестів: жодних реальних запитів до API провайдерів."""

import pytest
import requests


class NetworkBlocked(RuntimeError):
    """Тест спробував піти в мережу — потрібна підміна клієнта або фікстура."""


@pytest.fixture(autouse=True)
def _block_network(monkeypatch):
    """Будь-який HTTP через requests у тестах падає (а код зі `except Exception` лише залогує помилку)."""

    def blocked(self, request, **kwargs):
        raise NetworkBlocked(f"Мережа в тестах заборонена: {request.method} {request.url}")

    monkeypatch.setattr(requests.Session, "send", blocked)
