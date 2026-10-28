from django.apps import AppConfig


class FuelConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fuel"
    verbose_name = "Пальне"

    def ready(self):
        from .sqlite_unicode import install

        install()