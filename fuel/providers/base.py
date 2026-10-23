from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from django.utils import timezone


@dataclass
class NormalizedTx:
    provider: str
    external_id: str
    timestamp: datetime  # aware
    kind: str = "fuel"
    card_ref: str = ""
    vehicle: str = ""
    fuel: str = ""
    liters: Decimal | None = None
    price: Decimal | None = None
    amount: Decimal | None = None
    station: str = ""
    canceled: bool = False
    raw: dict = field(default_factory=dict)


def to_dec(value) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def aware(dt: datetime) -> datetime:
    """Naive datetime з API вважаємо локальним часом (Europe/Kyiv)."""
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


class BaseProvider:
    name = ""

    def fetch(self, date_from: date, date_to: date) -> list[NormalizedTx]:
        """date_to включно."""
        raise NotImplementedError

    # Фаза 2 (керування картками):
    # def block_card(self, card_ref): ...
    # def unblock_card(self, card_ref): ...
    # def set_limit(self, card_ref, limit): ...
