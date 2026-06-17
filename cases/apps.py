from django.apps import AppConfig


class CasesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cases"

    def ready(self):
        # Pin management-command reads to the primary (commands aren't covered
        # by ForcePrimaryReadsMiddleware). No-op until DATABASE_READ_URL is set.
        from config.db_router import install_management_command_primary_reads

        install_management_command_primary_reads()

        # Register models with auditlog
        from auditlog.registry import auditlog

        from cases.models import (
            Case,
            CaseEntityRelationship,
            DocumentSource,
            Feedback,
            JawafEntity,
        )

        auditlog.register(Case)
        auditlog.register(DocumentSource)
        auditlog.register(JawafEntity)
        # Through-model for case<->entity links: edited directly by the
        # /api/cases PATCH entities path (delete + recreate) and admin inlines.
        auditlog.register(CaseEntityRelationship)
        auditlog.register(Feedback)
