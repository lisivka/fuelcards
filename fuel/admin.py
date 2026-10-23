from django.contrib import admin

from .models import Card, ProviderConfig, Transaction


@admin.register(ProviderConfig)
class ProviderConfigAdmin(admin.ModelAdmin):
    list_display = ("code", "is_active", "edit_enabled")
    list_editable = ("is_active", "edit_enabled")


@admin.register(Card)
class CardAdmin(admin.ModelAdmin):
    list_display = ("provider", "number", "vehicle", "driver_name", "status", "status_updated_at")
    list_filter = ("provider", "status")
    search_fields = ("number", "vehicle", "driver_name", "note")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "provider", "kind", "card_ref", "fuel", "liters", "price", "amount", "station", "canceled")
    list_filter = ("provider", "kind", "canceled")
    search_fields = ("card_ref", "vehicle_raw", "station")
    date_hierarchy = "timestamp"
