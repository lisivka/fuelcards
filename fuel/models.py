from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.utils import timezone


class Provider(models.TextChoices):
    PETROLINE = "petroline", "Petroline"
    UPG = "upg", "UPG"
    UKRNAFTA = "ukrnafta", "UkrNafta"


class CardStatus(models.IntegerChoices):
    # Значення збігаються з полем status в API UkrNafta і пишуться в базу без перетворень.
    # Статуси Petroline (3 і 5) перекладаються на ці значення в fuel/providers/petroline_cards.py
    ACTIVE = 0, "Активна"
    BLOCKED = 1, "Заблокована"
    INACTIVE = 2, "Неактивна"
    REPLACED = 3, "Замінена"


class VehicleType(models.Model):
    """Тип авто (міксер, тягач тощо). Довідник веде адміністратор в адмінці; лише в нашій базі."""

    name = models.CharField("Назва", max_length=60, unique=True)

    class Meta:
        verbose_name = "Тип авто"
        verbose_name_plural = "Типи авто"
        ordering = ["name"]

    def __str__(self):
        return self.name

    def clean(self):
        super().clean()
        self.name = " ".join((self.name or "").split())
        duplicate = VehicleType.objects.filter(name__iexact=self.name).exclude(pk=self.pk)
        if self.name and duplicate.exists():
            raise ValidationError({"name": "Такий тип авто вже є."})


class Card(models.Model):
    provider = models.CharField("Постачальник", max_length=20, choices=Provider.choices)
    # Ключ зв'язку з транзакціями. Для Petroline це «Ідентифікатор», а не номер на картці
    number = models.CharField("Номер картки", max_length=64)
    ext_number = models.CharField("Номер на картці", max_length=64, blank=True)
    external_id = models.CharField("ID у провайдера", max_length=32, blank=True)
    vehicle = models.CharField("Авто / водій", max_length=120, blank=True)
    driver_name = models.CharField("ПІБ водія", max_length=120, blank=True)
    status = models.PositiveSmallIntegerField(
        "Статус картки",
        choices=CardStatus.choices,
        null=True,
        blank=True,
        db_index=True,
    )
    status_updated_at = models.DateTimeField("Останнє оновлення статусу", null=True, blank=True)
    note = models.CharField("Примітка", max_length=200, blank=True)
    # Лише в нашій базі: у провайдерів не пишеться і синхронізацією не змінюється
    vehicle_type = models.ForeignKey(
        VehicleType,
        verbose_name="Тип авто",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cards",
    )
    # Прив'язка картки у провайдера (зараз лише Petroline; заповнюється кнопкою «Оновити карти»).
    # Тип: 1 Персональна, 3 Техпролив, 5 Автомобільна — визначає, які поля редагуються і куди пишуться.
    provider_card_type = models.PositiveSmallIntegerField("Тип картки у провайдера", null=True, blank=True)
    provider_person_id = models.CharField("ID водія у провайдера", max_length=32, blank=True, db_index=True)
    provider_transport_id = models.CharField("ID транспорту у провайдера", max_length=32, blank=True)
    provider_label = models.CharField("Водій/авто у провайдера", max_length=200, blank=True)
    # Знімок лімітів на пальне з останнього оновлення карток (fuel/limits.py): UkrNafta — ліміти картки,
    # Petroline — ліміти її водія. Порожній список — лімітів немає або ще не читали (див. limits_updated_at)
    fuel_limits = models.JSONField("Ліміти на пальне", default=list, blank=True)
    limits_updated_at = models.DateTimeField("Ліміти оновлено", null=True, blank=True)

    class Meta:
        verbose_name = "Картка"
        verbose_name_plural = "Картки"
        ordering = ["provider", "number"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "number"], name="uniq_card_provider_number")
        ]

    def __str__(self):
        return f"{self.get_provider_display()} {self.display_number} {self.vehicle} {self.driver_name}".strip()

    @property
    def display_number(self):
        """Номер, який надрукований на картці (якщо відомий), інакше ключовий номер."""
        return self.ext_number or self.number

    @property
    def limits_short(self) -> str:
        """Скорочення лімітів на пальне: «Д500 М500»."""
        from .limits import short

        return short(self.fuel_limits)


# Кольори заливки рядків за замовчуванням; адміністратор змінює їх у «Конфігурації провайдера».
DEFAULT_ROW_COLORS = {
    Provider.UKRNAFTA: "#e9e9e9",  # світло-сірий
    Provider.PETROLINE: "#fde0e0",  # світло-червоний
    Provider.UPG: "#dff3df",  # світло-зелений
}

# Лише #RRGGBB: значення вставляється в CSS сторінок, тож довільний текст не допускаємо.
hex_color_validator = RegexValidator(r"^#[0-9a-fA-F]{6}$", "Колір у форматі #RRGGBB.")

DEFAULT_SYNC_DAYS = 2
MAX_SYNC_DAYS = 366  # межа одного запуску; всередині період ріжеться на частини (services.CHUNK_DAYS)


def sync_period_error(date_from: date | None, date_to: date | None) -> str:
    """Текст помилки для періоду синхронізації «з/по» або порожній рядок, якщо період коректний.

    Порожній період (обидві дати не задані) вважається коректним для збереження в моделі;
    дія синхронізації окремо вимагає, щоб обидві дати були задані.
    """
    if date_from is None and date_to is None:
        return ""
    if date_from is None or date_to is None:
        return "Задайте обидві дати періоду: «з» і «по»."
    if date_from > date_to:
        return f"Дата «з» ({date_from:%d.%m.%Y}) пізніша за дату «по» ({date_to:%d.%m.%Y})."
    if date_to > timezone.localdate():
        return f"Дата «по» ({date_to:%d.%m.%Y}) у майбутньому."
    days = (date_to - date_from).days + 1
    if days > MAX_SYNC_DAYS:
        return f"Період {days} днів довший за допустимі {MAX_SYNC_DAYS}."
    return ""


class ProviderConfig(models.Model):
    code = models.CharField("Код провайдера", max_length=20, unique=True)
    is_active = models.BooleanField("Активний", default=True)
    edit_enabled = models.BooleanField("Можна змінювати картки", default=True)
    row_color = models.CharField(
        "Колір рядків",
        max_length=7,
        blank=True,
        default="",
        validators=[hex_color_validator],
        help_text="Заливка рядків карток і транзакцій цього провайдера. Порожньо — без заливки.",
    )
    sync_days = models.PositiveSmallIntegerField(
        "Глибина синхронізації, днів",
        default=DEFAULT_SYNC_DAYS,
        validators=[MinValueValidator(1), MaxValueValidator(MAX_SYNC_DAYS)],
        help_text="Для дії «Синхронізувати за N днів»: від сьогодні мінус N днів до сьогодні.",
    )
    sync_date_from = models.DateField(
        "Синхронізувати з",
        null=True,
        blank=True,
        help_text="Для дії «Синхронізувати за період з/по».",
    )
    sync_date_to = models.DateField("Синхронізувати по", null=True, blank=True)

    class Meta:
        verbose_name = "Конфігурація провайдера"
        verbose_name_plural = "Конфігурації провайдерів"
        ordering = ["code"]

    def __str__(self):
        return self.code

    def clean(self):
        super().clean()
        error = sync_period_error(self.sync_date_from, self.sync_date_to)
        if error:
            raise ValidationError({"sync_date_to": error})

    @classmethod
    def ensure_all(cls):
        configs = {}
        for code, _label in Provider.choices:
            config, _ = cls.objects.get_or_create(
                code=code,
                defaults={
                    "is_active": True,
                    "edit_enabled": True,
                    "row_color": DEFAULT_ROW_COLORS.get(code, ""),
                },
            )
            configs[code] = config
        return configs

    @classmethod
    def row_colors(cls) -> dict[str, str]:
        """{code: "#rrggbb"} лише для провайдерів із коректно заданим кольором."""
        colors = {}
        for code, config in cls.ensure_all().items():
            try:
                hex_color_validator(config.row_color)
            except ValidationError:
                continue
            colors[code] = config.row_color.lower()
        return colors


class Transaction(models.Model):
    class Kind(models.TextChoices):
        FUEL = "fuel", "Заправка"
        TOPUP = "topup", "Поповнення"

    provider = models.CharField(max_length=20, choices=Provider.choices, db_index=True)
    external_id = models.CharField(max_length=64)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.FUEL)
    card = models.ForeignKey(Card, null=True, blank=True, on_delete=models.SET_NULL, related_name="transactions")
    card_ref = models.CharField(max_length=64, blank=True)
    vehicle_raw = models.CharField(max_length=200, blank=True)
    timestamp = models.DateTimeField(db_index=True)
    fuel = models.CharField(max_length=80, blank=True)
    liters = models.DecimalField(max_digits=12, decimal_places=3, null=True, blank=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    station = models.CharField(max_length=200, blank=True)
    canceled = models.BooleanField(default=False)
    raw = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Транзакція"
        verbose_name_plural = "Транзакції"
        ordering = ["-timestamp"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "external_id"], name="uniq_provider_tx")
        ]

    def __str__(self):
        return f"{self.provider}:{self.external_id}"

    @property
    def vehicle_display(self):
        if self.card and self.card.vehicle:
            return self.card.vehicle
        return self.vehicle_raw

class AuditLog(models.Model):
    """Журнал змін: хто, коли, що змінив і чи вдалося (зокрема записи в API провайдерів)."""

    class Action(models.TextChoices):
        CARD_CREATE = "card_create", "Створення картки"
        CARD_INFO = "card_info", "Зміна даних картки"
        CARD_STATUS = "card_status", "Зміна статусу картки"
        CARD_DELETE = "card_delete", "Видалення картки"
        TX_SYNC = "tx_sync", "Синхронізація транзакцій"
        CARDS_SYNC = "cards_sync", "Оновлення карток"
        LIMITS_SYNC = "limits_sync", "Оновлення лімітів"

    # Звідки запущено синхронізацію (details["source"])
    SOURCE_BUTTON = "button"
    SOURCE_ADMIN_DAYS = "admin_days"
    SOURCE_ADMIN_PERIOD = "admin_period"
    SOURCE_COMMAND = "command"
    SOURCE_LABELS = {
        SOURCE_BUTTON: "кнопка на сторінці",
        SOURCE_ADMIN_DAYS: "адмінка, за N днів",
        SOURCE_ADMIN_PERIOD: "адмінка, за період з/по",
        SOURCE_COMMAND: "команда manage.py",
    }
    CARD_ACTIONS = (
        Action.CARD_CREATE,
        Action.CARD_INFO,
        Action.CARD_STATUS,
        Action.CARD_DELETE,
    )
    SYNC_ACTIONS = (Action.TX_SYNC, Action.CARDS_SYNC, Action.LIMITS_SYNC)

    # Підписи полів для читабельного опису змін
    FIELD_LABELS = {
        "number": "Номер",
        "vehicle": "Авто",
        "driver_name": "ПІБ водія",
        "note": "Примітка",
        "status": "Статус",
        "vehicle_type": "Тип авто",
    }

    created_at = models.DateTimeField("Коли", auto_now_add=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Користувач",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="+",
    )
    # Знімок імені: запис лишається зрозумілим, навіть якщо користувача видалять
    username = models.CharField("Логін", max_length=150, blank=True)
    action = models.CharField("Дія", max_length=20, choices=Action.choices)
    card = models.ForeignKey(
        Card,
        verbose_name="Картка",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
    )
    card_label = models.CharField("Картка (знімок)", max_length=200, blank=True)
    provider = models.CharField("Постачальник", max_length=20, blank=True)
    changes = models.JSONField("Зміни", default=dict, blank=True)  # {"поле": [було, стало]}
    # Підсумок синхронізації: source, date_from, date_to (ISO), chunks, created, updated, capped
    details = models.JSONField("Деталі", default=dict, blank=True)
    via_api = models.BooleanField("Через API провайдера", default=False)
    success = models.BooleanField("Успішно", default=True)
    error = models.TextField("Помилка", blank=True)

    class Meta:
        verbose_name = "Запис аудиту"
        verbose_name_plural = "Журнал аудиту"
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.created_at:%d.%m.%Y %H:%M} {self.username} {self.get_action_display()} {self.card_label}"

    @property
    def who(self) -> str:
        """Хто виконав дію; для запусків командою користувача немає."""
        if self.username:
            return self.username
        if (self.details or {}).get("source") == self.SOURCE_COMMAND:
            return "система (команда)"
        return "—"

    def details_display(self) -> list[str]:
        """Підсумок синхронізації рядками для сторінки «Логи»."""
        d = self.details or {}
        lines = []
        if d.get("date_from") and d.get("date_to"):
            lines.append(f"Період: {_fmt_date(d['date_from'])} .. {_fmt_date(d['date_to'])}")
        if "created" in d or "updated" in d:
            lines.append(f"Нових: {d.get('created', 0)}, оновлено: {d.get('updated', 0)}")
        if d.get("failed"):
            lines.append(f"З помилкою: {d['failed']}")
        if d.get("chunks", 0) > 1:
            lines.append(f"Частин: {d.get('chunks_done', d['chunks'])} з {d['chunks']}")
        if d.get("capped"):
            lines.append("Початок обмежено останніми 31 днями")
        if d.get("source"):
            lines.append(f"Запуск: {self.SOURCE_LABELS.get(d['source'], d['source'])}")
        return lines

    def changes_display(self) -> list[str]:
        """Зміни у вигляді «Авто: «старе» → «нове»»."""
        lines = []
        for field, values in (self.changes or {}).items():
            old, new = (values + [None, None])[:2] if isinstance(values, list) else (None, values)
            label = self.FIELD_LABELS.get(field, field)
            if field == "status":
                old, new = _status_label(old), _status_label(new)
            lines.append(f"{label}: «{old or ''}» → «{new or ''}»")
        return lines


def _fmt_date(value: str) -> str:
    try:
        return date.fromisoformat(value).strftime("%d.%m.%Y")
    except (TypeError, ValueError):
        return str(value)


def _status_label(value) -> str:
    if value is None or value == "":
        return "—"
    try:
        return CardStatus(int(value)).label
    except ValueError:
        return str(value)
