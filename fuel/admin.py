from django import forms
from django.contrib import admin, messages
from django.db import transaction
from django.db.models import Count
from django.utils.html import format_html

from .card_service import record_audit
from .managers import assignable_managers
from .models import AuditLog, Card, ProviderConfig, Transaction, VehicleType
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
        source = AuditLog.SOURCE_ADMIN_DAYS if mode == MODE_DAYS else AuditLog.SOURCE_ADMIN_PERIOD
        for message in sync_configs(queryset.order_by("code"), mode, user=request.user, source=source):
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


@admin.register(VehicleType)
class VehicleTypeAdmin(admin.ModelAdmin):
    """Довідник типів авто. Під час видалення тип у карток стає порожнім — кожна така зміна йде в журнал."""

    list_display = ("name", "cards_count")
    search_fields = ("name",)

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_cards_count=Count("cards"))

    @admin.display(description="Карток", ordering="_cards_count")
    def cards_count(self, obj: VehicleType) -> int:
        return obj._cards_count

    def delete_model(self, request, obj: VehicleType) -> None:
        self._audit_cleared(request, [obj])
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset) -> None:
        self._audit_cleared(request, list(queryset))
        super().delete_queryset(request, queryset)

    def _audit_cleared(self, request, types: list[VehicleType]) -> None:
        with transaction.atomic():
            for vehicle_type in types:
                for card in vehicle_type.cards.all():
                    changes = {"vehicle_type": [vehicle_type.name, None]}
                    record_audit(AuditLog.Action.CARD_INFO, card, request.user, changes=changes)


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    list_display = (
        "provider",
        "number",
        "external_id",
        "vehicle",
        "vehicle_type",
        "manager",
        "driver_name",
        "status",
        "status_updated_at",
    )
    list_filter = ("provider", "status", "vehicle_type", "manager")
    list_select_related = ("vehicle_type", "manager")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        # Менеджером можна призначити лише активного користувача з групи «Менеджери»
        if db_field.name == "manager":
            kwargs["queryset"] = assignable_managers()
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    search_fields = ("number", "external_id", "vehicle", "driver_name", "note")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = (
        "timestamp",
        "provider",
        "kind",
        "card_ref",
        "fuel",
        "liters",
        "price",
        "amount",
        "station",
        "canceled",
    )
    list_filter = ("provider", "kind", "canceled")
    search_fields = ("card_ref", "vehicle_raw", "station")
    date_hierarchy = "timestamp"


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Журнал лише для перегляду: записи створює застосунок, правити чи видаляти їх не можна."""

    list_display = ("created_at", "who", "action", "card_label", "provider", "via_api", "success")
    list_filter = ("action", "provider", "via_api", "success")
    search_fields = ("username", "card_label", "error")
    date_hierarchy = "created_at"
    readonly_fields = [f.name for f in AuditLog._meta.fields]

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False
