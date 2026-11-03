from django import forms
from django.contrib import admin
from django.utils.html import format_html

from .models import Card, ProviderConfig, Transaction


@admin.register(ProviderConfig)
class ProviderConfigAdmin(admin.ModelAdmin):
    list_display = ("code", "is_active", "edit_enabled", "color_swatch", "row_color")
    list_editable = ("is_active", "edit_enabled", "row_color")

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
