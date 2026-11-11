from django.apps import AppConfig


class ProductionConfig(AppConfig):
    name = 'apps.production'

    def ready(self):
        from . import signals  # noqa: F401
