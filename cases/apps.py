from django.apps import AppConfig


class CasesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "cases"

    def ready(self):

        # Register models with auditlog. ``register_audited`` both registers the
        # model AND swaps its manager so bulk ``QuerySet.update()`` writes (the
        # pod-ORM / management-command pattern) are captured, not just saves.
        from jawafdehi_shared.db.audited import register_audited

        from cases.models import (
            Case,
            CaseCourtCaseReference,
            CaseEntityRelationship,
            CaseMaterialReference,
            ChatUserIdentity,
            Feedback,
        )

        register_audited(Case)
        # JawafEntity + DocumentSource were removed (NES owns entities, NGM owns
        # documents). Audit the Case<->NES-entity, Case<->NGM-material, and
        # Case<->court-case binds so evidence/relationship/reference changes
        # stay tracked.
        register_audited(CaseEntityRelationship)
        register_audited(CaseMaterialReference)
        register_audited(CaseCourtCaseReference)
        # Public feedback + chat-identity mapping. Both carry PII, so the
        # submitter's contact details / free text / OWUI identifiers are masked
        # in the change diff (auditlog stores a redaction marker, not the value)
        # — we track WHO changed WHAT (e.g. triage status), not personal content.
        register_audited(
            Feedback, mask_fields=["description", "contact_info", "ip_address"]
        )
        register_audited(
            ChatUserIdentity, mask_fields=["owui_user_id", "owui_user_name"]
        )
        # NOTE: CaseStateChange (itself an append-only audit trail) and
        # StatisticsSnapshot (a derived cache) are intentionally NOT audited.

        # Wire live unified-search indexing signals (best-effort, on_commit).
        from . import signals  # noqa: F401
