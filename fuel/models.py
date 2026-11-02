from django.db import models


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


class ProviderConfig(models.Model):
    code = models.CharField("Код провайдера", max_length=20, unique=True)
    is_active = models.BooleanField("Активний", default=True)
    edit_enabled = models.BooleanField("Можна змінювати картки", default=True)

    class Meta:
        verbose_name = "Конфігурація провайдера"
        verbose_name_plural = "Конфігурації провайдерів"
        ordering = ["code"]

    def __str__(self):
        return self.code

    @classmethod
    def ensure_all(cls):
        configs = {}
        for code, _label in Provider.choices:
            config, _ = cls.objects.get_or_create(
                code=code,
                defaults={"is_active": True, "edit_enabled": True},
            )
            configs[code] = config
        return configs


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