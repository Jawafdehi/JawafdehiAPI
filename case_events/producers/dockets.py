# SPDX-License-Identifier: Hippocratic-3.0
"""Docket signals, read out of the NGM lake the scrapers already write.

No scraper changes. ``scrape_courtcases`` and ``scrape_worker`` keep filling the
lake exactly as they do now, and this reads what they wrote. That separation is
worth keeping: the scrapers are the fragile part (CAPTCHAs, portal HTML, session
cookies) and they should not also carry a broker dependency.

**Stateless, by an overlapping window.** Every run rescans the last N hours of
lake rows and re-emits whatever it finds. There is no watermark, no checkpoint,
and no new table. :mod:`case_events.producers` has the full reasoning; the short
version is that the deduplication making this safe already exists and has to work
anyway, and a watermark that silently skips a range is a failure nobody notices
for months.

The window must comfortably exceed the cron interval. Twice the period is the
rule of thumb — enough that a skipped run, a slow drain or a clock nudge cannot
open a gap, since a gap here is a permanently missed fact while a duplicate is
free.

**Two of the three docket subjects are implemented.**
``jaw.signal.docket.status.changed`` is not, and cannot be from this vantage: the
lake stores a case's *current* status with no history, so "changed" is
unanswerable without the prior value. Detecting it needs either a history table
or a snapshot, both of which are the stateful design this producer exists to
avoid. A status change that matters almost always arrives as a hearing row or a
verdict anyway, so the gap is narrower than it looks — but it is a gap, not an
oversight.
"""

from __future__ import annotations

from datetime import timedelta

import structlog
from django.utils import timezone

from case_events import subjects
from case_events.producers import emit

logger = structlog.get_logger(__name__)

PRODUCER = "producer:dockets"

#: Default lookback. The docket cron is expected to run every 6h, so 48h is
#: eight times the period — deliberately generous, because the only cost of
#: overlap is duplicate messages the dedup spine drops, and the cost of a gap is
#: a fact that is never proposed.
DEFAULT_WINDOW_HOURS = 48

#: Hard ceiling on rows examined per kind per run. A backfill that suddenly
#: inserts a million hearings must not turn into a million messages; it is
#: better to emit a bounded batch, say so, and let the next run continue.
DEFAULT_LIMIT = 5000


def _courtcase_iri(court_id: str, case_number: str) -> str:
    """The canonical court-case ``@id``.

    Built with the shared helper, never by hand: ``build_courtcase_iri``
    lowercases both segments, so a hand-formatted
    ``.../courtcase/special/082-CR-0154`` matches nothing the matcher holds.
    """
    from jawafdehi_shared.entities.ids import build_courtcase_iri

    return build_courtcase_iri(court_id, case_number)


def hearing_signals(since, limit: int = DEFAULT_LIMIT):
    """Yield ``(subject, payload, refs, dedup_key, occurred_at)`` for new hearings.

    Keyed on ``created_at`` rather than ``hearing_date_ad``: a hearing scheduled
    for next month is news the day we scrape it, not the day it happens, and a
    hearing backfilled from an old cause list is news now.
    """
    from courts.models import CourtCaseHearing

    rows = (
        CourtCaseHearing.objects.filter(created_at__gte=since)
        .select_related("court")
        .order_by("created_at")[:limit]
    )
    for row in rows:
        iri = _courtcase_iri(row.court_id, row.case_number)
        payload = {
            "court": row.court_id,
            "case_number": row.case_number,
            "hearing_date_bs": row.hearing_date_bs,
            "hearing_date_ad": row.hearing_date_ad.isoformat() if row.hearing_date_ad else "",
            "bench": row.bench or "",
            "judge_names": row.judge_names or "",
            "case_status": row.case_status or "",
            "decision_type": row.decision_type or "",
            "remarks": row.remarks or "",
        }
        # The BS date, not the row's pk. The pk is ours and changes on re-import;
        # the docket date is the fact's own identity, so a re-scraped hearing
        # dedups against the proposal already staged for it.
        yield (
            subjects.SIGNAL_DOCKET_HEARING_ADDED,
            payload,
            [iri],
            f"docket:{iri}:hearing:{row.hearing_date_bs}",
            row.hearing_date_ad,
        )


def verdict_signals(since, limit: int = DEFAULT_LIMIT):
    """Yield signals for cases that have gained a verdict.

    Filtered on ``updated_at`` because a verdict lands as an UPDATE to an
    existing case row, not an insert — which is also why this cannot use
    ``created_at`` the way hearings do.

    Over-emits by construction: any touch of a case that already has a verdict
    re-yields it. That is the overlapping-window trade working as intended, and
    the dedup key is stable across those touches so nothing downstream doubles.
    """
    from courts.models import CourtCase

    rows = (
        CourtCase.objects.filter(
            updated_at__gte=since,
            verdict_date_ad__isnull=False,
            is_deleted=False,
        )
        .exclude(verdict_type__isnull=True)
        .exclude(verdict_type="")
        .order_by("updated_at")[:limit]
    )
    for row in rows:
        iri = _courtcase_iri(row.court_id, row.case_number)
        payload = {
            "court": row.court_id,
            "case_number": row.case_number,
            "verdict_type": row.verdict_type or "",
            "verdict_date_bs": row.verdict_date_bs or "",
            "verdict_date_ad": row.verdict_date_ad.isoformat() if row.verdict_date_ad else "",
            "verdict_judge": row.verdict_judge or "",
            "case_status": row.case_status or "",
            "case_subject": row.case_subject or "",
        }
        # Keyed on the verdict DATE, so a correction to the judge or the type
        # re-proposes rather than being swallowed — those are exactly the edits a
        # caseworker would want to see a second time. A verdict date changing is
        # a different fact and should re-propose.
        stamp = row.verdict_date_bs or (row.verdict_date_ad.isoformat() if row.verdict_date_ad else "")
        yield (
            subjects.SIGNAL_DOCKET_VERDICT_ENTERED,
            payload,
            [iri],
            f"docket:{iri}:verdict:{stamp}",
            row.verdict_date_ad,
        )


def scan(window_hours: int = DEFAULT_WINDOW_HOURS, limit: int = DEFAULT_LIMIT):
    """Every docket signal in the window, as an iterator. Reads only."""
    since = timezone.now() - timedelta(hours=window_hours)
    yield from hearing_signals(since, limit)
    yield from verdict_signals(since, limit)


def publish_window(window_hours: int = DEFAULT_WINDOW_HOURS, limit: int = DEFAULT_LIMIT) -> dict[str, int]:
    """Emit every signal in the window. Returns per-subject counts sent."""
    counts: dict[str, int] = {}
    for subject, payload, refs, dedup_key, occurred_at in scan(window_hours, limit):
        sent = emit(
            subject,
            producer=PRODUCER,
            payload=payload,
            subject_refs=refs,
            dedup_key=dedup_key,
            source=refs[0] if refs else "",
            occurred_at=_as_datetime(occurred_at),
        )
        key = subject if sent else f"{subject} (not sent)"
        counts[key] = counts.get(key, 0) + 1
    return counts


def _as_datetime(value):
    """A ``date`` from the lake as an aware datetime, or None.

    The envelope's ``occurred_at`` is when the FACT happened, and for a docket
    that is a court date rather than the moment we scraped it — which is the
    number an audit of enrichment lag actually needs.
    """
    if value is None:
        return None
    from datetime import date, datetime, timezone as dt_timezone

    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=dt_timezone.utc)
    return None
