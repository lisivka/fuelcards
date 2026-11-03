from django import forms
from django.contrib import admin, messages
from django.utils.html import format_html

from .models import Card, ProviderConfig, Transaction
from .services import MODE_DAYS, MODE_PERIOD, sync_configs

MESSAGE_LEVELS = {
    "info": messages.INFO,
    "success": messages.SUCCESS,
    "warning": messages.WARNING,
    "error": messages.ERROR,
}


@admin.register(ProviderConfig)
class ProviderConfigAdmin(admin.ModelAdmin):
    list_display = (
        "code",
        "is_active",
        "edit_enabled",
        "color_swatch",
        "row_color",
        "sync_days",
        "sync_date_from",
        "sync_date_to",
    )
    list_editable = ("is_active", "edit_enabled", "row_color", "sync_days", "sync_date_from", "sync_date_to")
    actions = ("sync_by_days", "sync_by_period")

    @admin.action(description="Синхронізувати вибраних за N днів (глибина з конфігурації)")
    def sync_by_days(self, request, queryset) -> None:
        self._run_sync(request, queryset, MODE_DAYS)

    @admin.action(description="Синхронізувати вибраних за період з/по (з конфігурації)")
    def sync_by_period(self, request, queryset) -> None:
        self._run_sync(request, queryset, MODE_PERIOD)

    def _run_sync(self, request, queryset, mode: str) -> None:
        for message in sync_configs(queryset.order_by("code"), mode):
            self.message_user(request, message.text, MESSAGE_LEVELS.get(message.level, messages.INFO))

    def formfield_for_dbfield(self, db_field, request, **kwargs):
        # Палітра браузера замість текстового поля: адміністратор просто вибирає колір.
        if db_field.name == "row_color":
            kwargs["widget"] = forms.TextInput(attrs={"type": "color"})
        return super().formfield_for_dbfield(db_field, request, **kwargs)

    @admin.display(description="Зразок")
    def color_swatch(self, obj: ProviderConfig) -> str:
        if not obj.row_color:
            return "—"
        return format_html(
            '<span style="display:inline-block;width:40px;height:16px;border:1px solid #999;'
            'background:{}"></span>',
            obj.row_color,
        )


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    list_display = ("provider", "number", "external_id", "vehicle", "driver_name", "status", "status_updated_at")
    list_filter = ("provider", "status")
    search_fields = ("number", "external_id", "vehicle", "driver_name", "note")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "provider", "kind", "card_ref", "fuel", "liters", "price", "amount", "station", "canceled")
    list_filter = ("provider", "kind", "canceled")
    search_fields = ("card_ref", "vehicle_raw", "station")
    date_hierarchy = "timestamp"
