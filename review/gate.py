"""The review verdict, read at the moment a case is published.

Before this module the review app's disposition (PASS / REVISE / REJECT) was a
dashboard number only: ``Case.publish()`` never read it, so a case could go live
with no review at all, or with its latest review saying REJECT.

``check_publish`` is the one place that reads it. What it does with the answer
depends on ``settings.REVIEW_GATE_MODE``:

* ``off``     -- read nothing, record nothing.
* ``warn``    -- the default. Never blocks. The verdict is returned so the
                 caller can store it on the PUBLISHED ``CaseStateChange`` row,
                 and anything other than a current PASS is logged. This is how
                 we learn whether published cases match what the judge said
                 before we let the judge block anyone.
* ``enforce`` -- no current review, or REJECT, raises. REVISE needs a written
                 reason. PASS proceeds.

"Current" means the latest finished review completed after the case's last
save. ``Case.submit()`` saves the case, so a review run while the case was a
DRAFT reads as stale once it is submitted. That is deliberate for ``enforce``:
it needs a review of what was actually submitted, which is why enforce waits on
auto-review-on-submit (a follow-up). In ``warn`` mode ``stale`` is only recorded,
alongside both timestamps, so the analysis can tell the two apart.
"""

import logging

from django.conf import settings
from django.core.exceptions import ValidationError

from review.models import CaseReview

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_WARN = "warn"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_WARN, MODE_ENFORCE)

PASS = "PASS"
REVISE = "REVISE"
REJECT = "REJECT"


def gate_mode() -> str:
    """The configured mode. An unknown value falls back to ``warn``, never to
    ``off``: a typo must not silently stop the record being kept."""
    mode = str(getattr(settings, "REVIEW_GATE_MODE", MODE_WARN)).strip().lower()
    return mode if mode in MODES else MODE_WARN


def latest_verdict(case) -> dict:
    """What the review app says about ``case`` right now. A pure read.

    Returns a JSON-safe dict:
      disposition         PASS / REVISE / REJECT, or None when no review finished
      review_id           the finished review it came from, or None
      overall_score       that review's weighted overall, or None
      gate_failures       how many hard-gate rules failed in it
      review_completed_at ISO time that review finished, or None
      case_updated_at     ISO time of the case's last save
      stale               True when there is no finished review, or the case was
                          saved after it finished
      newer_unfinished    status of a review created after it that has not
                          finished (pending / running / failed), else None
    """
    reviews = CaseReview.objects.filter(case=case)
    done = (
        reviews.filter(status=CaseReview.STATUS_DONE, completed_at__isnull=False)
        .order_by("-completed_at")
        .first()
    )
    newest = reviews.order_by("-created_at").first()
    newer_unfinished = None
    if newest is not None and (done is None or newest.pk != done.pk):
        if newest.status != CaseReview.STATUS_DONE:
            newer_unfinished = newest.status

    case_updated_at = case.updated_at.isoformat() if case.updated_at else None
    if done is None:
        return {
            "disposition": None,
            "review_id": None,
            "overall_score": None,
            "gate_failures": 0,
            "review_completed_at": None,
            "case_updated_at": case_updated_at,
            "stale": True,
            "newer_unfinished": newer_unfinished,
        }

    result = done.result or {}
    disposition = result.get("disposition")
    if disposition not in (PASS, REVISE, REJECT):
        disposition = None
    stale = bool(case.updated_at and done.completed_at < case.updated_at)
    return {
        "disposition": disposition,
        "review_id": done.pk,
        "overall_score": result.get("overall_score"),
        "gate_failures": len(result.get("gate_failures") or []),
        "review_completed_at": done.completed_at.isoformat(),
        "case_updated_at": case_updated_at,
        "stale": stale,
        "newer_unfinished": newer_unfinished,
    }


def _block_reason(verdict: dict, override_reason: str) -> str:
    """Why ``enforce`` would refuse this publish, or "" when it would allow it."""
    disposition = verdict["disposition"]
    if disposition is None or verdict["stale"]:
        return "Publishing needs a current review. Submit the case for review first."
    if disposition == REJECT:
        return "The latest review rejected this case. Fix it and review it again."
    if disposition == REVISE and not override_reason:
        return (
            "The latest review asked for revisions. Give a reason to publish anyway."
        )
    return ""


def check_publish(case, override_reason: str = "") -> dict | None:
    """Read the verdict for a case about to be published, and act on the mode.

    Returns the verdict (plus ``mode``, ``would_block`` and ``override_reason``)
    for the caller to store, or None in ``off`` mode. Raises ``ValidationError``
    only in ``enforce`` mode.
    """
    mode = gate_mode()
    if mode == MODE_OFF:
        return None

    override_reason = (override_reason or "").strip()
    verdict = latest_verdict(case)
    block = _block_reason(verdict, override_reason)
    verdict.update(
        mode=mode,
        would_block=bool(block),
        block_reason=block,
        override_reason=override_reason[:2000],
    )

    if block and mode == MODE_ENFORCE:
        raise ValidationError({"review": [block]})
    if block or verdict["disposition"] != PASS:
        logger.warning(
            "review gate (%s): case %s published with disposition=%s stale=%s: %s",
            mode,
            case.pk,
            verdict["disposition"],
            verdict["stale"],
            block or "allowed",
        )
    return verdict
