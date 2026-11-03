"""Клієнт карток Petroline (apiv2.petroline.in.ua). Етап А: лише читання списку карток."""

from ..models import CardStatus
from .petroline_api import PetrolineClient, PetrolineError

__all__ = ["PetrolineCards", "PetrolineError"]

# Статуси Petroline -> наші (CardStatus). Невідомі коди не вгадуємо: None.
STATUS_FROM_API = {3: CardStatus.ACTIVE.value, 5: CardStatus.BLOCKED.value}
TYPE_VEHICLE = 5  # «Автомобільна»: у картки поле Транспорт; в інших типів поле Водій


class PetrolineCards:
    """Список карток Petroline: номери, id, статус і назва водія/авто."""

    def __init__(self, client: PetrolineClient | None = None):
        self.client = client or PetrolineClient()

    def list_cards(self) -> list[dict]:
        records = self.client.post_paged(
            "/api/Keys/list",
            lambda skip, take: {"orderBy": [{"field": "id", "order": 0}], "skip": skip, "take": take},
        )
        return [row for row in map(self._normalize, records) if row]

    @staticmethod
    def _normalize(rec: dict) -> dict | None:
        """Лише потрібні поля; PIN та інше чутливе далі не передається."""
        number = str(rec.get("number") or "").strip()
        if not number:
            return None
        if rec.get("typeId") == TYPE_VEHICLE:
            names = (rec.get("transportName"), rec.get("transportNumber"), rec.get("personName"))
        else:
            names = (rec.get("personName"), rec.get("transportName"), rec.get("transportNumber"))
        label = next((" ".join(str(n).split()) for n in names if n and str(n).strip()), "")
        return {
            "number": number,
            "ext_number": str(rec.get("extNumber") or "").strip(),
            "external_id": str(rec.get("id") or ""),
            "api_status": rec.get("status"),
            "status": STATUS_FROM_API.get(rec.get("status")),
            "label": label,
        }
