"""The ``case_tags_tagger`` job kind: server-side hooks.

Tagging is a *job* for the same reason intent generation is (see
``case_proposals/job_kind.py``): a model call takes 30–90 seconds, and running it inside a
request or a bus consumer would hold something open for that long and re-run the model at
full cost on every retry. Enqueue-and-ack makes the retry unit a job, where the lease, the
retry budget and terminal handling already live.

Two hooks.

``build_payload`` runs SERVER-SIDE at claim time and resolves the case plus the current
vocabulary, so the worker needs no database. Resolving the vocabulary *here* rather than in
the worker is what lets a term the tagger minted on an earlier case be reused on this one.

``on_result`` validates and applies through :mod:`case_tags.write`. Unlike
``case_proposals``, there is no human between the model and the data — so everything the
prompt asks for is re-checked here, and a rejected tag is dropped and recorded rather than
coerced into something writable.

**One thing to know about ``on_result``:** ``jobs.queue.finalize`` swallows exceptions from
it and leaves the job DONE. So a rejected model answer would otherwise vanish — job green,
no tags, no trace. Everything here records its outcome onto ``job.result`` instead of
raising, which is what makes "the model returned nothing usable" distinguishable from "the
model tagged the case" in ``/api/jobs`` rather than both looking like success.
"""

from __future__ import annotations

from typing import Any

import structlog

from jobs.registry import KindSpec, register

logger = structlog.get_logger(__name__)

KIND = "case_tags_tagger"


def build_payload(job: Any) -> dict | None:
    """Resolve the case and the live vocabulary into the job payload."""
    from cases.models import Case  # noqa: PLC0415

    from case_tags.write import tagger_vocabulary  # noqa: PLC0415

    slug = (job.payload or {}).get("case_slug")
    if not slug:
        return None
    case = Case.objects.filter(slug=slug).first()
    if case is None:
        return None

    return {
        **(job.payload or {}),
        "case": {
            "slug": case.slug,
            "title": case.title,
            "short_description": getattr(case, "short_description", "") or "",
            "description": case.description or "",
            "key_allegations": list(getattr(case, "key_allegations", None) or []),
            "tags": list(case.tags or []),
        },
        "vocabulary": tagger_vocabulary(),
    }


def on_result(job: Any, data: dict) -> None:
    """Validate and apply. Never raises — records the outcome on ``job.result``."""
    from cases.models import Case  # noqa: PLC0415

    from case_tags.write import apply_tagger_output  # noqa: PLC0415

    slug = ((job.payload or {}).get("case") or {}).get("slug") or (job.payload or {}).get(
        "case_slug"
    )
    case = Case.objects.filter(slug=slug).first()
    if case is None:
        job.result = {**(job.result or {}), "tagging": {"error": f"no case {slug!r}"}}
        job.save(update_fields=["result"])
        return

    try:
        outcome = apply_tagger_output(case, data or {}, detected_by=f"job:{job.pk}")
    except Exception as exc:  # noqa: BLE001 - see the module docstring
        logger.warning("case_tags.tagger.apply_failed", case_slug=slug, error=str(exc))
        job.result = {**(job.result or {}), "tagging": {"error": str(exc)[:500]}}
        job.save(update_fields=["result"])
        return

    job.result = {
        **(job.result or {}),
        "tagging": {
            "applied": outcome.applied,
            "created_terms": outcome.created_terms,
            # Recorded, not just counted: "why was this refused" is the only question
            # anyone asks of a tagging run, and a count cannot answer it.
            "rejected": [{"value": v, "reason": r} for v, r in outcome.rejected],
        },
    }
    job.save(update_fields=["result"])
    logger.info(
        "case_tags.tagger.applied",
        case_slug=slug,
        applied=outcome.applied_ids,
        created=outcome.created_terms,
        rejected=len(outcome.rejected),
    )


def on_failure(job: Any) -> None:
    """Terminal give-up. Nothing to mutate — the tags are what would have changed."""
    logger.warning(
        "case_tags.tagger.gave_up",
        job_pk=job.pk,
        case_slug=(job.payload or {}).get("case_slug"),
    )


register(
    KindSpec(
        kind=KIND,
        build_payload=build_payload,
        on_result=on_result,
        on_failure=on_failure,
    )
)
