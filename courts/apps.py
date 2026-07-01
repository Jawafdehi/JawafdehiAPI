from django.apps import AppConfig


class CourtsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "courts"
    label = "courts"

    def ready(self):
        # Wire live unified-search indexing signals (best-effort, on_commit).
        from . import signals  # noqa: F401
