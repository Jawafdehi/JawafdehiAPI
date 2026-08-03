# SPDX-License-Identifier: Hippocratic-3.0
"""Worker-side handler for ``case_proposal_intent`` jobs.

The counterpart to :mod:`case_proposals.job_kind`: that module runs on the API
(resolving the case, staging the proposal), this one runs on the poller and does
nothing but turn a resolved payload into a model answer. It touches no database,
which is the whole point of the ``build_payload`` seam.

Handler signature is the poller's: ``handler(payload, *, on_stage) -> dict``.
"""

from __future__ import annotations

import time

import structlog

from llm import prompts as prompt_registry
from llm.exhaustion import is_exhaustion
from llm.templating import fence

logger = structlog.get_logger(__name__)

#: Budget for a SECOND attempt, used only after the first died of exhaustion.
#: Mirrors `courts.extract_verdicts`, including the ordering: the first attempt
#: always uses the spec's own budget, so the parameters an answer was produced
#: under are the parameters the spec documents. A first attempt at the larger
#: budget would be simpler and would quietly make the spec's `max_tokens` a
#: fiction.
#:
#: One retry, not a ladder. Exhaustion means the reasoning outran the allowance,
#: and 4x is a large enough step that a second failure is evidence about the
#: prompt rather than the budget — worth surfacing instead of spending through.
ESCALATED_MAX_TOKENS = 32_000

#: Cap on the fenced observation. A signal payload is small by construction, but
#: it can carry a scraped page excerpt, and a runaway one would otherwise push
#: the case snapshot out of the model's attention — the exact thing the prompt
#: needs it to read carefully.
MAX_OBSERVATION_CHARS = 20_000

#: Cap on the fenced case snapshot. Generous: the timeline is what the model
#: must check the observation against, and a snapshot trimmed here is a
#: duplicate entry proposed there. `job_kind._snapshot` already bounds the two
#: fields that actually grow, so this is a backstop, not the primary limit.
MAX_SNAPSHOT_CHARS = 60_000


def handle_case_proposal_intent(payload: dict, *, on_stage) -> dict:
    """Draft a change intent for one observed signal.

    Args:
        payload: The claimed job's payload. Carries ``case`` and ``language``
            (resolved server-side by ``job_kind.build_payload``) plus the
            ``observation`` the enqueuer recorded.
        on_stage: Best-effort progress ping; extends the job lease.

    Returns:
        The model's answer, plus the prompt identity that produced it. The
        ENTIRE dict becomes ``job.result`` and is handed to
        ``job_kind.on_result``, which is what validates it — nothing here
        decides whether the answer is usable.

    Raises:
        ValueError: if the payload is not one ``build_payload`` produced. The
            poller reports this as a retryable failure; it is really a bug, and
            it will exhaust its two attempts and dead-letter, which is visible.
    """
    case = payload.get("case")
    if not case:
        raise ValueError("case_proposal_intent payload is missing the resolved 'case' dict.")
    observation = payload.get("observation")
    if not observation:
        raise ValueError("case_proposal_intent payload is missing 'observation'.")

    context = {
        # Both fenced: the snapshot is archive prose that originated in scraped
        # court records, and the observation is a fact a producer read off a
        # portal. Neither is ours. See llm.templating.fence.
        "case_snapshot": fence(case, "case snapshot", max_chars=MAX_SNAPSHOT_CHARS),
        "observation": fence(observation, "observation", max_chars=MAX_OBSERVATION_CHARS),
    }

    # OMITTED, not passed as None, when build_payload did not resolve one. The
    # spec declares language as `required`, and that check tests for PRESENCE —
    # so `language=None` satisfies it, the {% if %} takes the else branch, and a
    # Nepali case silently gets an English prompt. Leaving the key out is what
    # makes the guard fire. (Found by a test that otherwise reached a real model
    # call, which is its own argument for the guard.)
    language = payload.get("language")
    if language:
        context["language"] = language

    spec = prompt_registry.get("case_proposal.intent")

    on_stage("prompting")
    started = time.monotonic()
    answer, escalated = _invoke_escalating(spec, context, on_stage=on_stage)
    duration = round(time.monotonic() - started, 3)

    if not isinstance(answer, dict):
        # invoke_json can return a list when the model wraps its object in one.
        # Passed through rather than coerced: on_result records "result is not
        # an object" against the job, which is more useful than a guess here.
        logger.warning(
            "case_proposal.intent_answer_not_an_object", got=type(answer).__name__
        )
        return {"intent": None, "rationale": "", "malformed_answer": True,
                "escalated": escalated, "duration_seconds": duration}

    return {
        "intent": answer.get("intent"),
        "confidence": answer.get("confidence"),
        "rationale": answer.get("rationale"),
        # Recorded on the job so a proposal that later turns out to be wrong can
        # be traced to the exact prompt text that drafted it.
        "prompt": {"name": spec.name, "version": spec.version, "tier": spec.tier},
        # Recorded because it is a cost signal, not a curiosity: a spec that
        # escalates routinely is a spec whose budget is wrong, and the only place
        # that is visible is on the jobs it produced.
        "escalated": escalated,
        "duration_seconds": duration,
    }


def _invoke_escalating(spec, context, *, on_stage):
    """Invoke ``spec``; on exhaustion, retry once at ``ESCALATED_MAX_TOKENS``.

    Returns ``(answer, escalated)``.

    Only exhaustion is retried. A malformed answer, a transport error or an auth
    failure is re-raised untouched — retrying those at 4x the budget would spend
    four times as much to fail identically, which is the trap
    :func:`llm.exhaustion.is_exhaustion` exists to avoid.
    """
    try:
        return spec.invoke(**context), False
    except Exception as exc:  # noqa: BLE001 - re-raised unless it is exhaustion
        if not is_exhaustion(exc):
            raise
        logger.warning(
            "case_proposal.intent_budget_exhausted",
            budget=spec.max_tokens,
            retrying_at=ESCALATED_MAX_TOKENS,
            error=str(exc)[:200],
        )
        # The lease is extended before paying for a second, slower call: the first
        # attempt has already spent the ack window's slack, and a job whose lease
        # expires mid-retry gets claimed by another poller and billed twice.
        on_stage("prompting (escalated)")
        return spec.invoke(max_tokens=ESCALATED_MAX_TOKENS, **context), True


#: kind -> worker-side handler, merged into the poller's HANDLERS registry.
HANDLERS = {"case_proposal_intent": handle_case_proposal_intent}
