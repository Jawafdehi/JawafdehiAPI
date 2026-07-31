# SPDX-License-Identifier: Hippocratic-3.0
"""The ``case_proposal_intent`` job kind: server-side hooks.

Intent generation is a *job*, not something a bus consumer does inline. A model
call takes 30–90 seconds; running it inside a consumer would hold a JetStream
message un-acked for that long, force a correspondingly long ``AckWait``, and
make every redelivery re-run the model at full cost. Enqueue-and-ack makes the
retry unit a job, which is where the lease, the retry budget and terminal
handling already live.

Three hooks, in the order they fire:

``build_payload`` runs SERVER-SIDE at claim time and resolves the case snapshot,
so the worker needs no database. This is the same seam ``case_review`` uses.

``on_result`` turns the model's answer into a PENDING
:class:`~case_proposals.models.CaseUpdateProposal` — through the same serializer
the HTTP create path uses, so a model cannot stage anything a caseworker could
not have posted by hand.

``on_failure`` records a terminal give-up. There is no domain row to mark failed
(the proposal is what would have been created), so it exists to make the
give-up visible rather than to mutate anything.

**One thing to know about ``on_result``:** ``jobs.queue.finalize`` swallows
exceptions from it and leaves the job DONE. So a rejected model answer would
otherwise vanish — job green, no proposal, no trace. Everything here therefore
records its outcome back onto ``job.result`` instead of raising, which is what
makes "the model declined" and "the model produced something unusable"
distinguishable in ``/api/jobs`` rather than both looking like success.
"""

from __future__ import annotations

import json
import re

import structlog
from django.conf import settings

from jobs.registry import KindSpec

logger = structlog.get_logger(__name__)

#: Job kind. Underscored, matching ``case_review`` / ``material_convert`` /
#: ``court_scrape``. The PROMPT registered for this work is dotted
#: (``case_proposal.intent``) because that is the prompt registry's convention.
KIND = "case_proposal_intent"

#: What this kind calls itself on the proposals it stages.
DETECTED_BY = "consumer:proposal-builder"

#: Intent types a MODEL may draft. Deliberately narrower than
#: ``SUPPORTED_INTENT_TYPES``: ``raw_patch`` carries an arbitrary JSON Patch, and
#: while ``case_proposals.apply`` allowlists the fields it can touch, letting a
#: model compose one widens the blast radius for no gain the two typed intents
#: don't already cover. A caseworker can still file one by hand.
MODEL_INTENT_TYPES = ("append_timeline_entry", "link_material")

#: Below this, a drafted intent is dropped rather than staged. The prompt also
#: instructs the model to decline below roughly this level; enforcing it here as
#: well is the difference between an instruction and a rule.
MIN_CONFIDENCE = 0.5

#: Caps on the snapshot handed to the model. Both are about cost and attention,
#: not correctness — but a truncated case is a case the model may wrongly think
#: has no matching timeline entry, so truncation is always marked.
MAX_DESCRIPTION_CHARS = 4000
MAX_TIMELINE_ENTRIES = 80

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")


def min_confidence() -> float:
    """The staging threshold. Overridable by settings so a pilot can tighten it.

    Read per call rather than captured at import, so ``override_settings`` and a
    live config change both take effect without a restart.
    """
    return float(getattr(settings, "CASE_PROPOSAL_MIN_CONFIDENCE", MIN_CONFIDENCE))


class BadIntentPayload(ValueError):
    """The job payload cannot produce a prompt. Not retryable."""


def _language_of(*texts: str) -> str:
    """``"np"`` if the case reads as Nepali, else ``"en"``.

    There is no language field on ``Case``, and inventing one for this would be
    the tail wagging the dog. A case whose own title and prose are in Devanagari
    should get a Nepali timeline entry appended to it; anything else gets
    English. Cheap, wrong occasionally, and wrong in a way a caseworker sees and
    can fix before it lands.
    """
    return "np" if any(_DEVANAGARI.search(t or "") for t in texts) else "en"


def _snapshot(case) -> dict:
    """The case as the model sees it.

    Built from the ORM rather than ``CaseDetailSerializer`` on purpose: the
    detail serializer resolves every linked material against NGM to attach
    display names and URLs, which is a cross-database fan-out this prompt has no
    use for. What the model needs is the prose, the timeline it must check
    against, and the identifiers.
    """
    description = case.description or ""
    truncated_description = len(description) > MAX_DESCRIPTION_CHARS

    timeline = list(case.timeline or [])
    dropped_entries = max(0, len(timeline) - MAX_TIMELINE_ENTRIES)

    return {
        "slug": case.slug,
        "title": case.title,
        "case_type": case.case_type,
        "state": case.state,
        "short_description": case.short_description or "",
        "description": description[:MAX_DESCRIPTION_CHARS],
        # Named, not silent: a model that cannot see the whole case must not be
        # told it can. Both flags are rendered into the fenced snapshot.
        "description_truncated": truncated_description,
        # The newest entries are the ones a fresh observation could duplicate,
        # so keep the TAIL when trimming, not the head.
        "timeline": timeline[-MAX_TIMELINE_ENTRIES:],
        "timeline_entries_omitted": dropped_entries,
        "linked_material_iris": [
            ref.material_iri for ref in case.material_references.all() if ref.material_iri
        ],
    }


def build_payload(job) -> dict:
    """Resolve the case snapshot for a claimed job. Runs with DB access.

    Raises:
        BadIntentPayload: if the job cannot name a case. Raised up-front, before
            any model call, so the worker fails it non-retryably.
    """
    from cases.models import Case

    payload = job.payload or {}
    case_id = payload.get("case_id")
    if not case_id:
        raise BadIntentPayload(f"{KIND} job payload is missing 'case_id'.")
    if not payload.get("observation"):
        raise BadIntentPayload(f"{KIND} job payload is missing 'observation'.")

    try:
        case = Case.objects.prefetch_related("material_references").get(pk=case_id)
    except Case.DoesNotExist:
        raise BadIntentPayload(f"No case with id {case_id}.") from None

    snapshot = _snapshot(case)
    return {
        "case": snapshot,
        "language": _language_of(case.title, case.short_description, case.description),
    }


def _json_safe(value):
    """Coerce a value the JSON column would reject into one it accepts.

    Not hypothetical: several of the fields recorded below are values a MODEL
    supplied, and ``json.dumps`` happily emits bare ``NaN``/``Infinity`` for
    floats — which is not JSON. sqlite rejects it with a ``JSON_VALID`` check
    failure and Postgres ``jsonb`` rejects it too, so a model answering
    ``"confidence": NaN`` would blow up the very bookkeeping meant to explain why
    its answer was refused, and the refusal would then be silent.
    """
    try:
        json.dumps(value, allow_nan=False)
        return value
    except (TypeError, ValueError):
        return repr(value)[:200]


def _record(job, **fields) -> None:
    """Write the hook's own outcome back onto ``job.result``.

    ``finalize`` has already committed the row and released its lock by the time
    a hook runs, and it swallows whatever a hook raises. Without this, a rejected
    answer leaves a DONE job, no proposal, and nothing to explain the gap.
    """
    try:
        result = dict(job.result or {})
        result["staged"] = {key: _json_safe(value) for key, value in fields.items()}
        job.result = result
        job.save(update_fields=["result", "updated_at"])
    except Exception:  # noqa: BLE001 - bookkeeping must not mask the real outcome
        logger.warning("case_proposal.intent_record_failed", job_id=job.pk, exc_info=True)


def _validation_failure(job, reason: str, **context) -> None:
    logger.warning("case_proposal.intent_rejected", job_id=job.pk, reason=reason, **context)
    _record(job, proposal_id=None, rejected=reason, **context)


def _already_staged(dedup_key: str) -> bool:
    """True if this fact has already been proposed, whatever was decided about it.

    Deliberately not filtered by status. A REJECTED proposal is a caseworker
    saying no to this exact fact, and the rejection has to stay sticky — a
    re-observed docket entry must not come back a week later as a fresh pending
    item for someone to reject again.
    """
    from case_proposals.models import CaseUpdateProposal

    return CaseUpdateProposal.objects.filter(dedup_key=dedup_key).exists()


def on_result(job, result: dict) -> None:
    """Stage the model's drafted intent as a PENDING proposal.

    Every rejection path records why and returns; none of them raise. See the
    module docstring for why.
    """
    from case_proposals.serializers import CaseUpdateProposalSerializer

    payload = job.payload or {}

    if not isinstance(result, dict):
        return _validation_failure(job, "result is not an object")

    intent = result.get("intent")
    rationale = str(result.get("rationale") or "")[:500]

    if intent is None:
        # The declined case. A normal, correct outcome — the prompt asks for it
        # whenever the observation is already recorded or too vague to state.
        logger.info("case_proposal.intent_declined", job_id=job.pk, rationale=rationale)
        return _record(job, proposal_id=None, declined=True, rationale=rationale)

    if not isinstance(intent, dict):
        return _validation_failure(job, "intent is neither an object nor null")

    itype = intent.get("type")
    if itype not in MODEL_INTENT_TYPES:
        return _validation_failure(job, "intent type not draftable by a model", intent_type=str(itype)[:60])

    try:
        confidence = float(result.get("confidence"))
    except (TypeError, ValueError):
        return _validation_failure(job, "confidence is missing or not a number")
    if not 0.0 <= confidence <= 1.0:
        return _validation_failure(job, "confidence out of range", confidence=confidence)
    threshold = min_confidence()
    if confidence < threshold:
        logger.info(
            "case_proposal.intent_below_threshold",
            job_id=job.pk,
            confidence=confidence,
            threshold=threshold,
        )
        return _record(job, proposal_id=None, below_threshold=True, confidence=confidence)

    case = payload.get("case") or {}
    dedup_key = payload.get("dedup_key")
    if not dedup_key:
        return _validation_failure(job, "job payload carries no dedup_key")

    # Checked BEFORE the serializer, because the serializer reports a duplicate
    # dedup_key as an ordinary field error and this hook would then file "the
    # fact is already known" under "the model produced something unusable".
    # Those need to stay distinguishable: the first is the idempotency spine
    # working, the second is a prompt regression, and conflating them means a
    # dashboard full of validation failures that are all benign.
    if _already_staged(dedup_key):
        logger.info("case_proposal.intent_already_staged", job_id=job.pk, dedup_key=dedup_key)
        return _record(job, proposal_id=None, duplicate=True, dedup_key=dedup_key)

    serializer = CaseUpdateProposalSerializer(
        data={
            # The snapshot's slug, not the enqueuer's: build_payload resolved the
            # case by its stable pk, so this is current even if it was re-slugged
            # between the observation and the claim.
            "case_slug": case.get("slug") or payload.get("case_slug") or "",
            "case_title": (case.get("title") or "")[:200],
            "source_kind": payload.get("source_kind") or "",
            "intent": intent,
            "confidence": confidence,
            "source": payload.get("source") or "",
            "detected_by": payload.get("detected_by") or DETECTED_BY,
            "dedup_key": dedup_key,
            "origin_subject": payload.get("origin_subject") or "",
            "origin_msg_id": payload.get("origin_msg_id") or "",
            "subject_refs": payload.get("subject_refs") or [],
        }
    )
    if not serializer.is_valid():
        # The serializer is the same gate the HTTP create path uses, so this is
        # exactly "a caseworker could not have posted this either".
        return _validation_failure(job, "failed proposal validation", errors=str(serializer.errors)[:500])

    try:
        proposal = serializer.save()
    except Exception as exc:  # noqa: BLE001 - most likely the unique dedup_key
        # A duplicate dedup_key means the same fact is already staged (or was
        # already decided, and the rejection is meant to be sticky). Not an
        # error — it is the idempotency spine doing its job.
        logger.info(
            "case_proposal.intent_not_staged",
            job_id=job.pk,
            dedup_key=dedup_key,
            error=str(exc)[:200],
        )
        return _record(job, proposal_id=None, duplicate=True, dedup_key=dedup_key)

    logger.info(
        "case_proposal.intent_staged",
        job_id=job.pk,
        proposal_id=proposal.pk,
        case_slug=proposal.case_slug,
        intent_type=itype,
        confidence=confidence,
    )
    # Announce it, so the notifier can tell a caseworker there is something
    # waiting. Best-effort — a staged proposal nobody could announce is still a
    # staged proposal, and the queue UI shows it either way.
    #
    # Guarded here as well as inside the publisher. The publisher already
    # swallows a failed envelope build, but not an import error or a broken
    # module, and an escape at this point would skip the _record() below: the
    # proposal would exist while its job showed no trace of having staged one.
    try:
        from case_proposals.publish import schedule_proposed_event

        schedule_proposed_event(proposal)
    except Exception:  # noqa: BLE001 - announcing must not cost us the record
        logger.warning("case_proposal.announce_failed", proposal_id=proposal.pk, exc_info=True)

    _record(job, proposal_id=proposal.pk, intent_type=itype, confidence=confidence)


def on_failure(job) -> None:
    """A terminal give-up. Nothing to mark — there is no row yet — so say so."""
    logger.warning(
        "case_proposal.intent_job_failed",
        job_id=job.pk,
        status=job.status,
        attempts=job.attempts,
        case_id=(job.payload or {}).get("case_id"),
        dedup_key=(job.payload or {}).get("dedup_key"),
        error=(job.error or "")[:500],
    )


SPEC = KindSpec(
    kind=KIND,
    # A premium-tier call is 30–90s; the worker heartbeats and extends this.
    lease_seconds=300,
    # Two, matching case_review's reasoning: a model that produced unusable
    # output for this input will usually do it again, and each attempt costs a
    # full premium call. Transient provider errors get one retry, not three.
    max_attempts=2,
    build_payload=build_payload,
    on_result=on_result,
    on_failure=on_failure,
)
