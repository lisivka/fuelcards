from django.db import models


class Provider(models.TextChoices):
    PETROLINE = "petroline", "Petroline"
    UPG = "upg", "UPG"
    UKRNAFTA = "ukrnafta", "UkrNafta"


class CardStatus(models.IntegerChoices):
    BLOCKED = 0, "Заблокована"
    ACTIVE = 1, "Активна"


class Card(models.Model):
    provider = models.CharField("Постачальник", max_length=20, choices=Provider.choices)
    number = models.CharField("Номер картки", max_length=64)
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
        return f"{self.get_provider_display()} {self.number} {self.vehicle} {self.driver_name}".strip()


class ProviderConfig(models.Model):
    code = models.CharField("Код провайдера", max_length=20, unique=True)
    is_active = models.BooleanField("Активний", default=True)
    edit_enabled = models.BooleanField("Можна змінювати картки", default=True)

    class Meta:
        verbose_name = "Конфігурація провайдера"
        verbose_name_plural = "Конфігурації провайдерів"
        ordering = ["code"]

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

    def __str__(self):
        return self.code


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

    @property
    def vehicle_display(self):
        if self.card and self.card.vehicle:
            return self.card.vehicle
        return self.vehicle_raw

    def __str__(self):
        return f"{self.provider}:{self.external_id}"
