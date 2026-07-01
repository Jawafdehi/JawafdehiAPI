from django.apps import AppConfig


class MaterialsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "materials"
    label = "materials"

    def ready(self):
        # Wire live unified-search indexing signals (best-effort, on_commit).
        from . import signals  # noqa: F401
