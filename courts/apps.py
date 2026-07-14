from django.apps import AppConfig


class CourtsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "courts"
    label = "courts"

    def ready(self):
        # Audit the NGM court layer. CourtCase (~1.6M) / CourtCaseHearing (~5.2M)
        # are bulk-COPY-loaded and frozen, so live ORM writes are rare; the
        # audited manager's row cap (AUDIT_BULK_UPDATE_MAX_ROWS) bounds any large
        # ``update()``, and bulk COPY loads stay unaudited by design (ORM-level
        # capture only). LogEntry rows are written cross-DB to ``default``.
        from jawafdehi_shared.db.audited import register_audited

        from courts.models import (
            BlacklistedFirm,
            CaseEntity,
            Court,
            CourtCase,
            CourtCaseHearing,
        )

        register_audited(Court)
        register_audited(CourtCase)
        register_audited(CourtCaseHearing)
        register_audited(CaseEntity)
        register_audited(BlacklistedFirm)

        # Wire live unified-search indexing signals (best-effort, on_commit).
        from . import signals  # noqa: F401
