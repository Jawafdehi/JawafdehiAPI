from django.apps import AppConfig


class CasesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cases"

    def ready(self):

        # Register models with auditlog
        from auditlog.registry import auditlog

        from cases.models import Case, DocumentSource, JawafEntity

        auditlog.register(Case)
        auditlog.register(DocumentSource)
        auditlog.register(JawafEntity)
