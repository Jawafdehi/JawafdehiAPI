from django.apps import AppConfig


class CaseProposalsConfig(AppConfig):
    """Case update proposals — the review-staging layer for case enrichment.

    Automation (or a caseworker) drafts a `CaseUpdateProposal`; a caseworker
    approves it, at which point its intent is applied to the Case via the
    sanctioned write path. Actor attribution on that Case write comes from
    ``AuditlogActorMixin`` on the viewset (the Case model is already
    auditlog-registered).
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "case_proposals"
    verbose_name = "Case update proposals"

    def ready(self):
        # Audit the proposal lifecycle itself: the CREATE entry captures the
        # proposed-change ``intent``; the approve/reject UPDATE entry captures
        # the acceptor (``LogEntry.actor``, bound per-request by
        # ``AuditlogActorMixin`` on the viewset) alongside the status
        # transition. ``register_audited`` registers auditlog AND swaps the
        # manager so bulk ``QuerySet.update()`` writes are logged too.
        from jawafdehi_shared.db.audited import register_audited

        from case_proposals.models import CaseUpdateProposal

        register_audited(CaseUpdateProposal)

        # Registers case_proposal.intent in the prompt registry. Imported purely
        # for the side effect: llm.prompts.get() RAISES for an unregistered name
        # (deliberately — there is no sensible default prompt), so without this
        # the intent job would fail at invoke time, on a worker, after a job had
        # already been claimed.
        from case_proposals import prompts  # noqa: F401
