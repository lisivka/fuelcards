from django.apps import AppConfig


class FuelConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "fuel"
    verbose_name = "Пальне"

    def ready(self):
        from django.contrib.auth import get_user_model
        from django.db.models.signals import pre_delete

        from .managers import audit_user_deleted
        from .sqlite_unicode import install

        install()
        pre_delete.connect(
            audit_user_deleted, sender=get_user_model(), dispatch_uid="fuel_audit_user_deleted"
        )
