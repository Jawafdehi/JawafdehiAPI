from django.apps import AppConfig


class CasesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cases"

    def ready(self):

        # Register models with auditlog
        from auditlog.registry import auditlog

        from cases.models import Case, CaseEntityRelationship, DocumentSource

        auditlog.register(Case)
        auditlog.register(DocumentSource)
        # JawafEntity was removed (NES owns entities). Audit the Case<->NES-entity
        # bind instead so entity-relationship changes stay tracked.
        auditlog.register(CaseEntityRelationship)

        # Wire live unified-search indexing signals (best-effort, on_commit).
        from . import signals  # noqa: F401
