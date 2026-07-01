from django.apps import AppConfig


class EntitiesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "entities"
    label = "entities"

    def ready(self):
        # Wire live unified-search indexing signals (best-effort, on_commit).
        from . import signals  # noqa: F401
