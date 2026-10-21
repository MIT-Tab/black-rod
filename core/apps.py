from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        # Django registers signal handlers when the app is ready.
        import core.signals  # noqa: F401  # pylint: disable=import-outside-toplevel,unused-import
