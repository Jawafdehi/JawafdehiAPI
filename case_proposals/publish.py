# SPDX-License-Identifier: Hippocratic-3.0
"""Publishing proposal decisions to the event bus.

This is the first real producer on ``jaw.case.>``, and it closes the loop the
design draws: a committed update is itself an event, so downstream consumers
(notify a caseworker, refresh stats, re-index) react to a decision instead of
polling for one.

Everything here is best-effort and fires ``on_commit``, mirroring the existing
``_schedule_reindex`` / ``_schedule_material_visibility`` hooks next door. Two
reasons it must be after commit rather than inside the transaction: a subscriber
that reacts instantly would otherwise be able to read the case *before* the
write is visible, and a rolled-back transaction would have already announced a
decision that never happened.
"""

from __future__ import annotations

import structlog
from django.db import transaction

from events import bus, subjects
from events.envelope import build_envelope

logger = structlog.get_logger(__name__)

#: What this producer calls itself in the envelope.
PRODUCER = "platform"

_SUBJECT_BY_STATUS = {
    "approved": subjects.CASE_UPDATE_APPROVED,
    "rejected": subjects.CASE_UPDATE_REJECTED,
}


def _case_iri(slug: str) -> str:
    """The case's ``@id``, or "" if it can't be built.

    Never raises: a malformed slug must not stop the event, and a missing ref is
    a degraded message rather than no message.
    """
    try:
        from jawafdehi_shared.entities.ids import build_case_iri

        return build_case_iri(slug)
    except Exception:  # noqa: BLE001 - a bad ref must not cost us the event
        logger.warning("case_proposal.case_iri_failed", case_slug=slug)
        return ""


def build_decision_envelope(proposal) -> dict:
    """The envelope for an approve/reject decision on ``proposal``."""
    subject = _SUBJECT_BY_STATUS[proposal.status]
    case_iri = _case_iri(proposal.case_slug)

    # The case first, then whatever the producer recorded — deduplicated and
    # order-preserving so the join key a consumer needs is at a stable position.
    refs = [ref for ref in [case_iri, *(proposal.subject_refs or [])] if ref]

    return build_envelope(
        subject=subject,
        producer=PRODUCER,
        subject_refs=list(dict.fromkeys(refs)),
        # Keyed on the DECISION, not the fact. proposal.dedup_key identifies the
        # underlying fact and is carried in the payload; this one exists so that
        # re-publishing the same decision collapses in JetStream. A proposal is
        # decided at most once, so pk + status is genuinely unique.
        dedup_key=f"proposal:{proposal.pk}:{proposal.status}",
        source=proposal.source,
        occurred_at=proposal.reviewed_at,
        payload={
            "proposal_id": proposal.pk,
            "case_slug": proposal.case_slug,
            "case_title": proposal.case_title,
            "status": proposal.status,
            "intent": proposal.intent,
            "confidence": proposal.confidence,
            "source_kind": proposal.source_kind,
            "detected_by": proposal.detected_by,
            "fact_dedup_key": proposal.dedup_key,
            "reviewer": proposal.reviewer,
            "review_notes": proposal.review_notes,
            "origin_msg_id": proposal.origin_msg_id,
        },
    )


def schedule_decision_event(proposal) -> None:
    """Publish the decision once the surrounding transaction commits.

    Best-effort in both directions: it never raises, and with ``NATS_URL`` unset
    it does nothing at all. An approval must succeed whether or not a broker is
    reachable — that property is the point, and is worth an explicit test.
    """
    if proposal.status not in _SUBJECT_BY_STATUS:
        return

    # Snapshot now, publish later: on_commit runs after the transaction, and
    # reading a mutated-or-refetched instance then would risk publishing state
    # that isn't what was decided.
    envelope = build_decision_envelope(proposal)
    subject = envelope["subject"]

    def _run():
        try:
            bus.publish(subject, envelope)
        except Exception:  # noqa: BLE001 - the bus is never allowed to be fatal
            logger.warning(
                "case_proposal.publish_failed",
                subject=subject,
                proposal_id=envelope["payload"]["proposal_id"],
            )

    transaction.on_commit(_run)
