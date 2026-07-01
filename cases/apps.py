from django.apps import AppConfig


class CasesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cases"

    def ready(self):

        # Register models with auditlog
        from auditlog.registry import auditlog

        from cases.models import (
            Case,
            CaseEntityRelationship,
            CaseMaterialReference,
        )

        auditlog.register(Case)
        # JawafEntity + DocumentSource were removed (NES owns entities, NGM owns
        # documents). Audit the Case<->NES-entity and Case<->NGM-material binds
        # instead so evidence/relationship changes stay tracked.
        auditlog.register(CaseEntityRelationship)
        auditlog.register(CaseMaterialReference)

        # Wire live unified-search indexing signals (best-effort, on_commit).
        from . import signals  # noqa: F401
