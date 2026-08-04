# SPDX-License-Identifier: Hippocratic-3.0
"""The four consumers, and what each does with a message.

Each handler is an ordinary synchronous function of an envelope. It runs in a
worker thread (the runner's event loop cannot touch the Django ORM), it must be
idempotent because delivery is at-least-once, and it signals its outcome by
returning or raising — see :class:`case_events.consumers.PoisonMessage`.

The pipeline, and where the human sits in it::

    jaw.signal.>            matcher           which case is this about?
      -> jaw.case.matched   proposal-builder  enqueue an intent job
         -> (jobs queue)                      the model drafts; a row is staged
            -> jaw.case.update.proposed       notifier: a caseworker is told
               -> HUMAN APPROVES OR REJECTS
                  -> jaw.case.update.approved derive: downstream refresh

Nothing on that path writes a Case. The only thing that does is a caseworker
pressing approve, which runs ``case_proposals.apply`` — the same code path as an
interactive edit.

**One deliberate departure from DESIGN.md §6.3.** That table has the
proposal-builder publishing ``jaw.case.update.proposed``. It cannot: at the
moment the builder acks, no proposal exists — a job has been enqueued, and the
row appears a minute later when the model answers. Announcing a proposal that
does not exist yet would give the notifier a message it cannot resolve. So
``jaw.case.update.proposed`` is published where the row is actually created, by
:mod:`case_proposals.publish`, exactly as the approve/reject decisions already
are.
"""

from __future__ import annotations

import structlog

from case_events import subjects
from case_events.consumers import ConsumerSpec, PoisonMessage, register
from case_events.envelope import build_envelope

logger = structlog.get_logger(__name__)

#: Cap on how many cases one signal may match before we treat the match as
#: meaningless rather than merely ambiguous. A docket legitimately belongs to
#: several archive cases — the Bhatta money-laundering case spans five dockets
#: across three courts — but a ref matching dozens of cases is a bad ref, not a
#: rich one, and fanning out an LLM job per case would be expensive as well as
#: wrong.
MAX_MATCHES = 5


def _producer(name: str) -> str:
    return f"consumer:{name}"


# ── matcher ──────────────────────────────────────────────────────────────────


def _enrichable():
    """Cases an observed fact may be proposed against.

    Excludes CLOSED, which on this platform is the soft delete: ``Case.delete()``
    flips the state and keeps the row (accountability archive — nothing is hard
    deleted). Without this filter a deleted case still joins on its court-case
    references, so every scrape of that docket buys a premium model call and puts
    a review item in front of a caseworker for a case somebody deliberately
    removed. DRAFT and IN_REVIEW are deliberately kept: a case being built is
    exactly the sort of thing new facts should land on.
    """
    from cases.models import Case, CaseState

    return Case.objects.exclude(state=CaseState.CLOSED)


def _cases_for_refs(refs: list[str]):
    """Cases joined to any of ``refs`` by an EXACT identifier match.

    The joins used are the ones that are facts rather than guesses: the case's
    own ``@id``, a court-case reference, a linked material. All three are stable
    IRIs written by us.

    Entity references are deliberately NOT a join here. A signal naming a
    politician matches every case that politician appears in, which for a
    frequently-charged individual is most of the archive — it would turn one
    observation into dozens of proposals, none of them evidenced. Entity-based
    matching needs its own scoring, and it is not this consumer's pilot job.
    """
    if not refs:
        return []

    from django.db.models import Q

    return list(
        _enrichable()
        .filter(
            Q(courtcase_references__courtcase_iri__in=refs)
            | Q(material_references__material_iri__in=refs)
        )
        .distinct()
        .only("id", "slug", "title")[: MAX_MATCHES + 1]
    )


def _cases_named_directly(refs: list[str]):
    """Cases whose own ``@id`` appears in ``refs``.

    Separate from the join above because a signal that names the case outright
    is not a match at all — it is an assertion, and it should not be scored as
    though we inferred it.
    """
    from jawafdehi_shared.entities.ids import parse_case_iri

    slugs = []
    for ref in refs:
        try:
            slugs.append(parse_case_iri(ref).slug)
        except Exception:  # noqa: BLE001 - most refs are not case IRIs
            continue
    if not slugs:
        return []

    return list(_enrichable().filter(slug__in=slugs).only("id", "slug", "title"))


def handle_matcher(envelope: dict, context) -> None:
    """Resolve which case(s) a raw signal is about, and say so on the bus.

    Publishes one ``jaw.case.matched`` per case. Emitting one message per case
    rather than one message listing several keeps every downstream consumer's
    unit of work a single case, which is what makes their dedup keys and their
    retries meaningful.

    **A failed publish raises**, which is the one place this file departs from
    the best-effort publishing rule the rest of the codebase follows. Everywhere
    else the bus describes work that has already been done, so a dropped message
    costs a log line. Here the message IS the work: if the ``jaw.case.matched``
    does not land, the signal has been consumed and nothing downstream will ever
    hear about it — acked, unretried, and absent from the DLQ. ``bus.publish``
    returns False without raising for a suppressed connect and logs it at DEBUG,
    so ignoring the return silently discards facts during any broker blip.
    """
    from case_events import bus

    refs = [ref for ref in (envelope.get("subject_refs") or []) if isinstance(ref, str) and ref]
    if not refs:
        # Nothing to join on. Not poison — a producer emitting a ref-less signal
        # is a producer bug, and burying the message would hide it behind a DLQ
        # nobody reads. Acked and logged, because there is genuinely no work.
        logger.info("case_events.matcher_no_refs", subject=envelope.get("subject"))
        return

    direct = _cases_named_directly(refs)
    matched = direct or _cases_for_refs(refs)

    if not matched:
        logger.info("case_events.matcher_no_match", subject=envelope.get("subject"), refs=refs[:5])
        return

    if len(matched) > MAX_MATCHES:
        # Over the cap, so the ref is not identifying anything. Logged loudly and
        # acked: retrying will match exactly as many cases next time.
        logger.warning(
            "case_events.matcher_too_many_matches",
            subject=envelope.get("subject"),
            refs=refs[:5],
            count=len(matched),
        )
        return

    # A direct case @id is an assertion; a join through a court case or a
    # material is an inference, and one that gets weaker the more cases it hits.
    confidence = 1.0 if direct else round(1.0 / len(matched), 3)

    for case in matched:
        payload = {
            "case_id": case.pk,
            "case_slug": case.slug,
            "case_title": case.title,
            "match_confidence": confidence,
            "matched_on": "case_iri" if direct else "reference_iri",
            # The whole originating signal, carried forward: the proposal-builder
            # hands it to a model as the observation, and it is the only record
            # of what was actually seen.
            "signal": {
                "subject": envelope.get("subject"),
                "payload": envelope.get("payload"),
                "source": envelope.get("source"),
                "raw_ref": envelope.get("raw_ref"),
                "occurred_at": envelope.get("occurred_at"),
                "producer": envelope.get("producer"),
            },
        }
        matched_key = _matched_dedup_key(envelope, case.slug)
        # wait=True: without it a True return means "handed to the loop thread",
        # and the JetStream ack — including the rejection you get when no stream
        # claims the subject, i.e. when nats_bootstrap has not been run — arrives
        # later on a callback nothing here can see.
        sent = bus.publish(
            subjects.CASE_MATCHED,
            build_envelope(
                subject=subjects.CASE_MATCHED,
                producer=_producer("matcher"),
                subject_refs=list(dict.fromkeys([*refs])),
                # Derived from the SIGNAL's dedup key, not generated: re-matching
                # a redelivered signal must collapse rather than fan out. Scoped
                # by case so a multi-case match still produces distinct messages.
                dedup_key=matched_key,
                source=envelope.get("source") or "",
                raw_ref=envelope.get("raw_ref") or "",
                occurred_at=None,
                payload=payload,
            ),
            wait=True,
        )
        if not sent:
            # Retryable, not poison: the message is fine, the broker is not. The
            # redelivery re-publishes the cases already done in this loop, and
            # that is harmless — every one of them carries a deterministic
            # dedup_key, so JetStream collapses the repeats.
            raise RuntimeError(
                f"could not publish {subjects.CASE_MATCHED} for {case.slug!r} ({matched_key})"
            )

    logger.info(
        "case_events.matcher_matched",
        subject=envelope.get("subject"),
        cases=[c.slug for c in matched],
        confidence=confidence,
    )


def _matched_dedup_key(envelope: dict, case_slug: str) -> str:
    """A deterministic key for "this signal, matched to this case".

    Falls back to the subject plus the signal's own occurrence time when the
    producer supplied no dedup key. That is weaker — two genuinely distinct
    facts observed in the same second on the same subject would collide — but it
    is still deterministic, which is the property that matters: a redelivery
    must produce the same key, and a random one would defeat the whole spine.
    """
    base = envelope.get("dedup_key") or f"{envelope.get('subject')}:{envelope.get('occurred_at')}"
    return f"matched:{base}:{case_slug}"


# ── proposal-builder ─────────────────────────────────────────────────────────


def handle_proposal_builder(envelope: dict, context) -> None:
    """Enqueue an intent-generation job for one matched case, then ack.

    The model call is emphatically NOT made here. It takes 30–90 seconds, which
    would hold this message un-acked for the whole of it, force an ``AckWait``
    long enough to cover the worst case, and make every redelivery pay for a
    fresh premium call. Enqueue-and-ack hands all of that to the jobs queue,
    where the lease, the retry budget and terminal handling already exist.
    """
    from case_proposals.job_kind import DETECTED_BY, KIND
    from jobs import queue as jobs_queue

    payload = envelope.get("payload") or {}
    case_id = payload.get("case_id")
    if not case_id:
        # Structurally wrong, and no redelivery fixes it. Poison, so it lands in
        # the DLQ attributable rather than after five identical failures.
        raise PoisonMessage(f"{subjects.CASE_MATCHED} envelope has no payload.case_id")

    signal = payload.get("signal") or {}
    dedup_key = envelope.get("dedup_key") or ""
    if not dedup_key:
        raise PoisonMessage(f"{subjects.CASE_MATCHED} envelope has no dedup_key to key a proposal on")

    job = jobs_queue.enqueue(
        KIND,
        payload={
            "case_id": case_id,
            "case_slug": payload.get("case_slug") or "",
            "observation": signal,
            # The proposal's dedup key IS the matched-signal key. That is what
            # makes a re-observed fact collapse onto the existing proposal —
            # including a REJECTED one, which is how a rejection stays sticky.
            "dedup_key": dedup_key,
            "source_kind": _source_kind_for(signal.get("subject") or ""),
            "source": signal.get("source") or "",
            "detected_by": DETECTED_BY,
            "origin_subject": envelope.get("subject") or "",
            "origin_msg_id": dedup_key,
            "subject_refs": envelope.get("subject_refs") or [],
        },
        # The queue dedups on this too, so a redelivered match does not enqueue a
        # second identical job while the first is still queued or running.
        dedup_key=f"intent:{dedup_key}",
    )
    logger.info(
        "case_events.intent_job_enqueued",
        job_id=job.pk,
        case_id=case_id,
        dedup_key=dedup_key,
    )


#: Signal subject -> the ``SignalSource`` the staged proposal records. Kept here
#: rather than derived from the subject string so that renaming a subject is a
#: visible, one-line decision instead of a silent change of provenance.
_SOURCE_KIND_BY_SUBJECT = {
    subjects.SIGNAL_DOCKET_HEARING_ADDED: "ngm_docket",
    subjects.SIGNAL_DOCKET_VERDICT_ENTERED: "ngm_docket",
    subjects.SIGNAL_DOCKET_STATUS_CHANGED: "ngm_docket",
    subjects.SIGNAL_COURTORDER_PUBLISHED: "court_order",
    subjects.SIGNAL_CIAA_PRESSRELEASE: "ciaa_press",
    subjects.SIGNAL_NEWS_MATCHED: "news",
    subjects.SIGNAL_MANUAL_NOTE: "caseworker",
}


def _source_kind_for(subject: str) -> str:
    """The proposal's ``source_kind`` for a signal subject.

    Returns "" for an unmapped subject rather than guessing. The proposal
    serializer then rejects it, which records a validation failure against the
    job — visible, and pointing at the missing mapping.
    """
    return _SOURCE_KIND_BY_SUBJECT.get(subject, "")


# ── notifier ─────────────────────────────────────────────────────────────────


def handle_notifier(envelope: dict, context) -> None:
    """Tell a caseworker that a proposal needs them, or that one was decided.

    Two outputs, and the order is deliberate. The structured log line comes FIRST
    and unconditionally: it is the record that a transition happened, it answers
    "did anyone ever see this?", and it must not depend on an external service
    being reachable. The webhook is best-effort on top of it.

    **Mail was considered and ruled out.** Outbound mail from this application is
    disabled, and enabling it for automated per-proposal messages is a larger
    decision than this needs. A webhook reaches a channel people already watch and
    costs one POST.

    **What that POST may carry is constrained** — see :mod:`case_events.notify`.
    The short version: a PENDING proposal is unreviewed model output about named
    individuals, so the notification links to it rather than quoting it.

    A webhook failure is swallowed, never raised. Raising would redeliver the
    message, and redelivery would re-notify rather than re-do anything useful: the
    proposal row already exists and the queue is the surface that matters.
    """
    from case_events import notify

    payload = envelope.get("payload") or {}
    logger.info(
        "case_events.caseworker_notified",
        subject=envelope.get("subject"),
        proposal_id=payload.get("proposal_id"),
        case_slug=payload.get("case_slug"),
        status=payload.get("status"),
        confidence=payload.get("confidence"),
        reviewer=payload.get("reviewer"),
    )
    notify.post(payload)


# ── derive ───────────────────────────────────────────────────────────────────


def _resolve_case(slug: str):
    """A case by its current slug, falling back to a retired one. None if neither.

    A published case can be re-slugged operationally, and a decision envelope
    carries whatever slug was current when the proposal was decided — which for
    a message that has been retried across a rename is a slug that no longer
    resolves. ``CaseSlugHistory`` is the codebase's existing answer to exactly
    this (the retrieve path 301s through it, and review submissions resolve
    through it); consulting it here costs one indexed lookup on a miss and turns
    a dead-lettered message back into a completed one.
    """
    from cases.models import Case, CaseSlugHistory

    case = Case.objects.filter(slug=slug).first()
    if case is not None:
        return case
    # A LIVE slug always wins, so this is only ever reached on a genuine miss.
    retired = CaseSlugHistory.objects.filter(slug=slug).select_related("case").first()
    if retired is None:
        return None
    logger.info("case_events.derive_resolved_retired_slug", requested=slug, current=retired.case.slug)
    return retired.case


def handle_derive(envelope: dict, context) -> None:
    """Refresh what an approved change makes stale.

    Today that is the case's search document. ``case_proposals.apply`` already
    schedules a re-index in an ``on_commit`` hook, but that hook is best-effort
    and swallows its own failures — so an index write that fails during a
    deploy is lost with a warning and nothing retries it. Doing it from the bus
    gives the same work an at-least-once delivery budget and a DLQ, which is a
    real improvement rather than duplicated effort.

    **The statistics snapshot is deliberately NOT refreshed here.** It aggregates
    over the full NES/NGM datasets and takes 15–19 seconds on prod (see
    ``cases.services.statistics``); running that per approval would hold a
    message for most of its ack window and pile up under any burst. It stays on
    its schedule. Debouncing it — one deduplicated job per burst of approvals,
    which the jobs queue's ``dedup_key`` already makes easy — is the follow-up.
    """
    payload = envelope.get("payload") or {}
    case_slug = payload.get("case_slug")
    if not case_slug:
        raise PoisonMessage("approved-decision envelope has no payload.case_slug")

    case = _resolve_case(case_slug)
    if case is None:
        # Not found live, and not found under a retired slug either. Redelivery
        # will not find it, so bury rather than burn four more attempts.
        raise PoisonMessage(f"no case with slug {case_slug!r} to re-index")

    from cases.search_index import index_now

    # `index_now`, not `index`. They are the same function; `index` is wrapped in
    # @best_effort, which logs the transport error and returns None. Calling that
    # one made this handler unable to fail — so the retry budget and the DLQ that
    # justify doing the re-index from the bus at all were decoration, and this
    # consumer was the duplicated effort its docstring says it is not.
    index_now(case)
    logger.info("case_events.derived_reindex", case_slug=case_slug, proposal_id=payload.get("proposal_id"))


# ── registration ─────────────────────────────────────────────────────────────

register(
    ConsumerSpec(
        name="matcher",
        stream="SIGNALS",
        filter_subject=subjects.ALL_SIGNALS,
        handler=handle_matcher,
        description="Raw signals -> which case they concern -> jaw.case.matched",
    )
)

register(
    ConsumerSpec(
        name="proposal-builder",
        stream="CASE_EVENTS",
        filter_subject=subjects.CASE_MATCHED,
        handler=handle_proposal_builder,
        description="A matched signal -> a queued case_proposal_intent job",
    )
)

register(
    ConsumerSpec(
        name="notifier",
        stream="CASE_EVENTS",
        filter_subject=subjects.ALL_CASE_UPDATES,
        handler=handle_notifier,
        description="Proposal lifecycle transitions -> caseworker notification",
    )
)

register(
    ConsumerSpec(
        name="derive",
        stream="CASE_EVENTS",
        filter_subject=subjects.CASE_UPDATE_APPROVED,
        handler=handle_derive,
        description="An approved change -> re-index the case",
    )
)
