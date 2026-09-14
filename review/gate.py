# SPDX-License-Identifier: Hippocratic-3.0
"""The publish gate: does this case carry a CURRENT, PASSING review verdict?

Until this module existed, ``Case.publish()`` asked two questions — is the state
DRAFT/IN_REVIEW, are the required fields present — and never read the review
app's verdict. A case graded REJECT, or never graded at all, published with one
click; the 23-rule judge produced a number on a dashboard that nothing obeyed.

This module is the missing wire. It is deliberately small and read-only over
the review tables:

* :func:`latest_verdict` — the newest completed review for a case and whether
  the case has been edited since (a stale verdict is no verdict).
* :func:`enforce_publish_gate` — apply the verdict to a publish attempt under
  the configured mode.
* :func:`enqueue_review_on_submit` — make sure a case that reaches the publish
  button has a review to be judged by.

MODES (``settings.REVIEW_GATE_MODE``):

``off``      the gate does nothing. Test suites and emergencies.
``warn``     the gate evaluates and LOGS what it would have blocked, then lets
             the publish through. The rollout default: a judge outage cannot
             freeze publishing while the team learns what the gate says.
``enforce``  the gate blocks. REJECT and no/stale review raise; REVISE raises
             unless an override reason is supplied (and the caller records it).

The gate raises ``django.core.exceptions.ValidationError`` keyed on ``review``
so the existing API path surfaces it as a 422 with a field-keyed message, the
same way the required-field gates already do.
"""

import logging

from django.conf import settings
from django.core.exceptions import ValidationError

log = logging.getLogger("review.gate")

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_ENFORCE = "enforce"
_MODES = (MODE_OFF, MODE_WARN, MODE_ENFORCE)

DISPOSITION_PASS = "PASS"
DISPOSITION_REVISE = "REVISE"
DISPOSITION_REJECT = "REJECT"


def gate_mode():
    """The configured mode, read at call time so ``override_settings`` works.

    An unrecognised value is treated as ``warn`` — the safe side: the gate keeps
    evaluating and logging, and never silently turns itself off.
    """
    mode = (getattr(settings, "REVIEW_GATE_MODE", MODE_WARN) or MODE_WARN).lower()
    return mode if mode in _MODES else MODE_WARN


def auto_review_on_submit():
    return bool(getattr(settings, "REVIEW_AUTO_REVIEW_ON_SUBMIT", True))


def latest_verdict(case, *, edited_at=None):
    """Describe the newest COMPLETED review of ``case``.

    Returns a dict::

        {
          "review_id": int | None,
          "disposition": "PASS" | "REVISE" | "REJECT" | None,
          "overall_score": int | None,
          "gate_failures": [{key, title, score, gate_min}, ...],
          "reviewed_at": datetime | None,
          "stale": bool,       # True when the case changed after the review
          "reason": str,       # human-readable summary of why it is unusable
        }

    STALENESS is judged against the review's ``started_at`` (falling back to
    ``completed_at``): the job reads the case at claim time, so an edit made
    after the review STARTED was not seen by the judge, whatever it concluded.

    ``edited_at`` is when the case's CONTENT last changed. It defaults to
    ``case.updated_at``, which is right for direct model use — but the API's
    PATCH path bumps ``updated_at`` on every request, including a state-only
    ``/state -> PUBLISHED`` patch that edits nothing the judge graded. That path
    passes the pre-patch timestamp instead, so publishing a case does not by
    itself make its own review stale. A patch that DOES touch content passes
    nothing and gets the honest, post-edit answer.

    Only ``status == done`` rows count. A failed review (judge unreachable, see
    ``review.judge.JudgeUnavailable``) is deliberately NOT a verdict — the gate
    reports "no completed review" and the caseworker sees that the review must
    be re-run, rather than a REJECT they cannot act on.
    """
    from review.models import CaseReview

    review = (
        CaseReview.objects.filter(case=case, status=CaseReview.STATUS_DONE)
        .order_by("-completed_at", "-created_at")
        .first()
    )
    if review is None or not isinstance(review.result, dict):
        return {
            "review_id": None,
            "disposition": None,
            "overall_score": None,
            "gate_failures": [],
            "reviewed_at": None,
            "stale": True,
            "reason": "no completed review",
        }

    result = review.result
    seen_at = review.started_at or review.completed_at
    edited_at = edited_at if edited_at is not None else case.updated_at
    stale = bool(seen_at and edited_at and edited_at > seen_at)
    disposition = result.get("disposition")
    return {
        "review_id": review.pk,
        "disposition": disposition,
        "overall_score": result.get("overall_score"),
        "gate_failures": list(result.get("gate_failures") or []),
        "reviewed_at": review.completed_at,
        "stale": stale,
        "reason": "case edited after review" if stale else "",
    }


def _blocking_message(verdict):
    """Why the gate refuses, as one sentence a caseworker can act on."""
    if verdict["review_id"] is None:
        return (
            "This case has no completed review. Submit it for review and wait "
            "for the verdict before publishing."
        )
    if verdict["stale"]:
        return (
            "The case was edited after its last review; the verdict is stale. "
            "Re-run the review before publishing."
        )
    if verdict["disposition"] == DISPOSITION_REJECT:
        failed = ", ".join(g.get("key", "?") for g in verdict["gate_failures"])
        return (
            "The latest review verdict is REJECT"
            + (f" (failed gates: {failed})" if failed else "")
            + f", overall score {verdict['overall_score']}. Fix the flagged "
            "rules and re-run the review."
        )
    if verdict["disposition"] == DISPOSITION_REVISE:
        return (
            "The latest review verdict is REVISE "
            f"(overall score {verdict['overall_score']}). Publishing requires a "
            "written reason (X-Transition-Reason)."
        )
    return f"Unrecognised review disposition {verdict['disposition']!r}."


def evaluate_publish_gate(case, override_reason="", *, edited_at=None):
    """Decide, without raising, whether ``case`` may publish.

    Returns ``(allowed: bool, verdict: dict, message: str)``. ``message`` is
    empty when allowed. Pure; used by :func:`enforce_publish_gate` and by
    anything that wants to show the gate state (a UI, a warn-mode log).
    ``edited_at`` is forwarded to :func:`latest_verdict`.
    """
    verdict = latest_verdict(case, edited_at=edited_at)
    if verdict["review_id"] is None or verdict["stale"]:
        return False, verdict, _blocking_message(verdict)
    disposition = verdict["disposition"]
    if disposition == DISPOSITION_PASS:
        return True, verdict, ""
    if disposition == DISPOSITION_REVISE:
        if (override_reason or "").strip():
            return True, verdict, ""
        return False, verdict, _blocking_message(verdict)
    # REJECT, or anything the scorer never emits.
    return False, verdict, _blocking_message(verdict)


def enforce_publish_gate(case, override_reason="", *, edited_at=None):
    """Apply the gate to a publish attempt under the configured mode.

    Returns the verdict dict (so the caller can record which review it
    published against). Raises ``ValidationError({"review": [...]})`` only in
    ``enforce`` mode when the gate refuses. In ``warn`` mode a refusal is logged
    at WARNING with the case id, the mode, and the message — that log line is
    the evidence a team reads before flipping to ``enforce``.
    ``edited_at`` is forwarded to :func:`latest_verdict`.
    """
    mode = gate_mode()
    if mode == MODE_OFF:
        return latest_verdict(case, edited_at=edited_at)

    allowed, verdict, message = evaluate_publish_gate(
        case, override_reason, edited_at=edited_at
    )
    if allowed:
        return verdict
    if mode == MODE_WARN:
        log.warning(
            "review gate WOULD BLOCK publish (mode=warn) case=%s slug=%s "
            "disposition=%s stale=%s: %s",
            case.pk,
            case.slug,
            verdict["disposition"],
            verdict["stale"],
            message,
        )
        return verdict
    raise ValidationError({"review": [message]})


def enqueue_review_on_submit(case, *, submitted_by=None):
    """Queue a review for a case that was just submitted, if none is in flight.

    Returns the ``CaseReview`` created, or ``None`` when skipped. Skips when the
    feature is off, or when the case already has a pending/running review (the
    queue's own dedup key is per-review-row, so the row-level check here is what
    stops a resubmit from stacking a second identical grade).

    Never raises: a queue outage must not turn "submit for review" into an
    error the caseworker cannot act on. The failure is logged and the case is
    still IN_REVIEW; the gate will report "no completed review" at publish.
    """
    if not auto_review_on_submit():
        return None
    from review.models import CaseReview

    in_flight = CaseReview.objects.filter(
        case=case, status__in=(CaseReview.STATUS_PENDING, CaseReview.STATUS_RUNNING)
    ).exists()
    if in_flight:
        return None
    try:
        from review.views import _enqueue_review_job

        review = CaseReview.objects.create(
            case=case,
            case_title=(case.title or ""),
            case_state=(case.state or ""),
            status=CaseReview.STATUS_PENDING,
            submitted_by=submitted_by,
        )
        _enqueue_review_job(review, submitted_by=submitted_by)
        return review
    except Exception as exc:  # noqa: BLE001 - submit must not fail on queue trouble
        log.error(
            "auto-review enqueue failed for case=%s slug=%s: %s", case.pk, case.slug, exc
        )
        return None
