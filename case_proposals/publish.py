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

from case_events import bus, subjects
from case_events.envelope import build_envelope

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


def _producer_refs(proposal) -> list[str]:
    """``proposal.subject_refs``, defensively reduced to a list of non-empty strings.

    ``subject_refs`` is an unvalidated writable ``JSONField``, so it holds
    whatever a producer posted — including a scalar or a nested list. Iterating
    it blindly used to raise *inside the caller's transaction* and roll back the
    case write; see :func:`schedule_decision_event`. Anything unusable is
    dropped with a warning rather than propagated.
    """
    refs = proposal.subject_refs
    if not refs:
        return []
    if not isinstance(refs, (list, tuple)):
        logger.warning(
            "case_proposal.subject_refs_not_a_list",
            proposal_id=proposal.pk,
            got=type(refs).__name__,
        )
        return []

    clean = [ref for ref in refs if isinstance(ref, str) and ref]
    if len(clean) != len(refs):
        logger.warning(
            "case_proposal.subject_refs_partially_dropped",
            proposal_id=proposal.pk,
            kept=len(clean),
            given=len(refs),
        )
    return clean


def build_decision_envelope(proposal) -> dict:
    """The envelope for an approve/reject decision on ``proposal``.

    Raises:
        ValueError: if ``proposal.status`` is not a decision. Callers scheduling
            an event should go through :func:`schedule_decision_event`, which
            treats a non-decision as a no-op; reaching here with one is a bug in
            the caller, not a message to degrade.
    """
    try:
        subject = _SUBJECT_BY_STATUS[proposal.status]
    except KeyError:
        raise ValueError(
            f"Proposal {proposal.pk} has status {proposal.status!r}, which is not a "
            f"decision. Expected one of {sorted(_SUBJECT_BY_STATUS)}."
        ) from None

    case_iri = _case_iri(proposal.case_slug)

    # The case first, then whatever the producer recorded — deduplicated and
    # order-preserving so the join key a consumer needs is at a stable position.
    refs = [ref for ref in [case_iri, *_producer_refs(proposal)] if ref]

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


def build_proposed_envelope(proposal) -> dict:
    """The envelope announcing that a proposal now exists and awaits review.

    Published from HERE, where the row is created, and not from the bus's
    proposal-builder consumer as ``DESIGN.md`` §6.3 originally had it. The
    builder acks the moment it has enqueued an intent job — at which point no
    proposal exists; the row appears a minute later when the model answers. A
    ``jaw.case.update.proposed`` emitted then would name a proposal_id nothing
    could resolve. Announcing it where it is created is both correct and exactly
    how the approve/reject decisions already work.
    """
    case_iri = _case_iri(proposal.case_slug)
    refs = [ref for ref in [case_iri, *_producer_refs(proposal)] if ref]

    return build_envelope(
        subject=subjects.CASE_UPDATE_PROPOSED,
        producer=PRODUCER,
        subject_refs=list(dict.fromkeys(refs)),
        # A proposal is created once — pk alone is unique, and no status suffix
        # is wanted here: the whole point is that this fires exactly once, at
        # creation, whatever happens to the row afterwards.
        dedup_key=f"proposal:{proposal.pk}:proposed",
        source=proposal.source,
        occurred_at=proposal.created_at,
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
            "origin_msg_id": proposal.origin_msg_id,
        },
    )


def schedule_proposed_event(proposal) -> None:
    """Announce a newly-created proposal once the surrounding transaction commits.

    Same guarantees as :func:`schedule_decision_event`: never raises, no-ops
    with ``NATS_URL`` unset, and cannot cost the caller its write.
    """
    _schedule(proposal, build_proposed_envelope, what="proposed")


def schedule_decision_event(proposal) -> None:
    """Publish the decision once the surrounding transaction commits.

    Best-effort in both directions: it never raises, and with ``NATS_URL`` unset
    it does nothing at all. An approval must succeed whether or not a broker is
    reachable — that property is the point, and is worth an explicit test.
    """
    if proposal.status not in _SUBJECT_BY_STATUS:
        return

    _schedule(proposal, build_decision_envelope, what=proposal.status)


def _schedule(proposal, build, *, what: str) -> None:
    """Build an envelope now, publish it after the transaction commits.

    Snapshot now, publish later: on_commit runs after the transaction, and
    reading a mutated-or-refetched instance then would risk publishing state
    that isn't what was written.

    This runs INSIDE the caller's atomic block, so it must not raise. It used
    not to be guarded, and that was a real defect rather than a theoretical
    one: a proposal created with a non-list `subject_refs` (a writable,
    unvalidated JSONField) raised TypeError here, 500'd the approval and rolled
    back the case write — with no broker configured at all. The bus is not
    allowed to cost us a write, and "the bus" includes describing the write.
    """
    try:
        envelope = build(proposal)
    except Exception:  # noqa: BLE001 - a describable event beats a lost write
        logger.warning(
            "case_proposal.envelope_failed",
            proposal_id=proposal.pk,
            status=what,
            exc_info=True,
        )
        return

    subject = envelope["subject"]

    def _run():
        try:
            if not bus.publish(subject, envelope):
                # publish() returns False for a disabled bus (expected) and for a
                # dropped message (not). Logged either way: an event that never
                # reached the case-domain log is exactly what a later audit would
                # need to know about, and it is otherwise silent.
                logger.info(
                    "case_proposal.event_not_published",
                    subject=subject,
                    proposal_id=envelope["payload"]["proposal_id"],
                    bus_enabled=bus.enabled(),
                )
        except Exception:  # noqa: BLE001 - the bus is never allowed to be fatal
            logger.warning(
                "case_proposal.publish_failed",
                subject=subject,
                proposal_id=envelope["payload"]["proposal_id"],
            )

    transaction.on_commit(_run)
