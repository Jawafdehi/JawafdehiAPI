from django.apps import AppConfig


class JobsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "jobs"
    verbose_name = "Job Queue"

    def ready(self):
        # Import consumers' kind registrations so the registry is populated as
        # soon as the app is ready. Each domain app registers its own kind(s)
        # (handlers/hooks); the jobs app itself stays domain-agnostic.
        # Registration is import-time and idempotent.
        from . import registry  # noqa: F401
