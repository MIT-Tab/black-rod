from django.apps import AppConfig

class ApdaonlineConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apdaonline"

    def ready(self):
        import apdaonline.signals  
