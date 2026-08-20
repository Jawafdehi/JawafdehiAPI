"""Promote a decisive HEARING's verdict onto the case row it belongs to.

The companion backfill to the write-path fix in #459. That PR made
``upsert_causelist`` promote a disposing sitting onto the parent case row as the
cause list is ingested, which stops the drift going forward. It cannot repair
what already drifted: those cause-list dates are in the past, and re-crawling
them would mean re-fetching years of portal pages for data we already hold.

**The bug this repairs.** Enrichment is one-shot — ``courts.scraper.crawl``
selects candidates with ``.exclude(status="enriched")`` — and before #459 it was
the only writer of ``case_status``/``verdict_*``. A case first detail-scraped
BEFORE it was decided therefore kept ``case_status='चलिरहेको'`` with NULL verdict
columns permanently, while the listing scraper kept appending hearing rows. The
verdict was never missing from the database; it was only ever missing from the
case row. Measured on prod 2026-08-20: **114 Special Court cases**, up from 105 on
2026-08-04, so the set grew with every verdict until #459 landed. Their
dispositions are सफाई 46, आंशिक ठहर 37, ठहर 27, खारेज 3, and one with a NULL
``decision_type`` that is deliberately left alone.

``backfill_case_status`` cannot reach these. Its hearing fallback reads
``extra_data.enrichment_hearings``, which is frozen in that same one-shot
enrichment snapshot, never the ``court_case_hearings`` TABLE — and even when it
does fire it only writes ``verdict_type``/``verdict_date_*``, never
``case_status``, which is the field the public docket page renders. Hence a
separate command rather than another DQ rule bolted onto that one.

**What it writes, and what it refuses to.** Conservative by construction: it
FILLS gaps and corrects a contradiction, it does not restate the record.

- ``verdict_type`` — only when currently NULL.
- ``verdict_date_bs``/``verdict_date_ad`` — only when ``verdict_date_bs`` is NULL,
  and only as a pair. Same "never overwrite a date already present" rule as
  ``backfill_case_status`` DQ-03.
- ``case_status`` — replaced by the disposing sitting's own label ONLY when the
  stored value does not already parse as DECIDED. So ``चलिरहेको`` is corrected and
  an existing ``फैसला (२०८३/०३/२५)`` is left exactly as it is.

Classification is :func:`courts.case_status.outcome_from_hearings` — the same
function the write path uses — so this inherits its guarantees rather than
re-deriving them: terminal bucket only (the portal writes ``अन्तिम आदेश`` on
plainly interlocutory orders), an unrecognised ``decision_type`` yields nothing
rather than a guess, ``आंशिक`` is matched ahead of ``ठहर`` so a partial conviction
is never recorded as a full one, and where a case was decided, reopened on review
and decided again the LAST disposing sitting wins. Hearings are therefore fed in
ascending date order, which is what makes "last" mean "latest".

SAFETY: dry-run by default. It never writes unless BOTH ``--execute`` and
``--i-understand-this-writes-prod`` are passed. Prints a projection tally either
way, so the dry run is the review artifact.

    manage.py promote_hearing_verdicts --court special
    manage.py promote_hearing_verdicts --court special --case 081-CR-0111
    manage.py promote_hearing_verdicts --court special --execute --i-understand-this-writes-prod

Like ``backfill_case_status`` this writes via ``bulk_update``, which emits plain
UPDATEs and so bypasses ``post_save`` — the live OpenSearch upsert and the
auditlog entry do NOT fire. ``updated_at`` is bumped on every changed row, so
follow a real run with ``reindex_courtcases --since`` to re-index exactly the rows
touched.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from courts import case_status as cs
from courts.models import CourtCase, CourtCaseHearing

NGM_DB = "ngm"

# Mirrors backfill_case_status: bounds each UPDATE's CASE body while still
# collapsing thousands of round-trips into a handful.
_WRITE_BATCH_SIZE = 500


def compute_promotion(
    case: CourtCase, hearings: list[dict]
) -> tuple[dict[str, object], cs.HearingOutcome | None]:
    """``(column_updates, outcome)`` promoting ``hearings``' disposition onto ``case``.

    Pure — no DB access. An empty dict means nothing to do, and the returned
    ``outcome`` says which kind of nothing: ``None`` for "no sitting disposed of
    this case" (so it is not a candidate at all), non-``None`` for "the row already
    records what the hearings say" — which is what makes a second pass change zero
    rows.

    ``hearings`` must be in ASCENDING date order: ``outcome_from_hearings`` keeps
    the last disposing sitting, and "last" is only "latest" if the input is sorted.
    """
    outcome = cs.outcome_from_hearings(hearings)
    if outcome is None:
        return {}, None

    updates: dict[str, object] = {}

    # Fill a missing outcome; never restate or overwrite one already recorded.
    if not case.verdict_type:
        updates["verdict_type"] = outcome.verdict_type

    # Dates come from the disposing sitting, as a pair, and only into a gap. Note
    # these are the dates outcome_from_hearings derived from the sitting's BS
    # string — NOT the stored hearing_date_ad, which carries a 1900-01-01 sentinel
    # when a BS date would not convert (that column is NOT NULL).
    if not case.verdict_date_bs and outcome.verdict_date_bs:
        updates["verdict_date_bs"] = outcome.verdict_date_bs
        updates["verdict_date_ad"] = outcome.verdict_date_ad

    # The contradiction this command exists for: a decided case whose case_status
    # still reads as pending/unknown. Correct it to the sitting's own label, but
    # leave a status that already parses as DECIDED completely alone.
    parsed = cs.parse_case_status(case.case_status)
    if parsed.lifecycle_status != cs.DECIDED:
        decisive = _decisive_label(hearings)
        if decisive and decisive != case.case_status:
            updates["case_status"] = decisive

    return updates, outcome


def _decisive_label(hearings: list[dict]) -> str | None:
    """The ``case_status`` label of the LAST sitting that could have disposed.

    Kept in step with :func:`courts.case_status.outcome_from_hearings` by walking
    the same list in the same direction and keeping the last match.
    """
    label = None
    for hearing in hearings:
        status = " ".join(str(hearing.get("case_status") or "").split())
        if status in cs.HEARING_TERMINAL_STATUSES:
            label = status
    return label


def _candidate_case_numbers(court: str, *, using: str = NGM_DB) -> list[str]:
    """Case numbers in ``court`` holding at least one possibly-terminal sitting.

    A pre-filter, not a decision: a sitting outside
    :data:`~courts.case_status.HEARING_TERMINAL_STATUSES` can never dispose of a
    case, so its case never needs loading. Keeps the scan proportional to decided
    cases (3,084 for special) instead of the court's whole docket (13,165).
    """
    return sorted(
        set(
            CourtCaseHearing.objects.using(using)
            .filter(court_id=court, case_status__in=cs.HEARING_TERMINAL_STATUSES)
            .values_list("case_number", flat=True)
        )
    )


def _hearings_by_case(
    court: str, case_numbers: list[str], *, using: str = NGM_DB
) -> dict[str, list[dict]]:
    """All hearings for ``case_numbers``, ascending by date, as classifier dicts.

    ALL hearings are loaded, not just the terminal-looking ones, because a case can
    be decided, reopened on review and decided again — the classifier needs the
    full sequence to land on the operative disposition.

    ``hearing_date`` carries the BS string so the shared classifier derives the
    BS/AD pair through its own path, ignoring the stored ``hearing_date_ad`` and
    its 1900-01-01 sentinel.
    """
    grouped: dict[str, list[dict]] = {}
    rows = (
        CourtCaseHearing.objects.using(using)
        .filter(court_id=court, case_number__in=case_numbers)
        .order_by("case_number", "hearing_date_ad", "id")
        .values_list("case_number", "case_status", "decision_type", "hearing_date_bs")
    )
    for case_number, status, decision, date_bs in rows:
        grouped.setdefault(case_number, []).append(
            {"case_status": status, "decision_type": decision, "hearing_date": date_bs}
        )
    return grouped


def run_promotion(
    *, court: str, only: list[str] | None = None, batch_size: int = 500,
    limit: int | None = None, execute: bool = False, using: str = NGM_DB, on_batch=None,
) -> dict[str, int]:
    """Scan ``court``'s decided cases, tally (and optionally apply) promotions."""
    stats = {
        "candidates": 0,
        "scanned": 0,
        "rows_changed": 0,
        "case_status_fixed": 0,
        "verdict_type_set": 0,
        "verdict_date_set": 0,
        "no_classifiable_outcome": 0,
    }

    candidates = sorted(set(only)) if only else _candidate_case_numbers(court, using=using)
    stats["candidates"] = len(candidates)
    if limit:
        candidates = candidates[:limit]

    for start in range(0, len(candidates), batch_size):
        page = candidates[start : start + batch_size]
        hearings = _hearings_by_case(court, page, using=using)
        cases = list(
            CourtCase.objects.using(using)
            .filter(court_id=court, case_number__in=page)
            .only(
                "case_number", "court", "case_status", "verdict_type",
                "verdict_date_bs", "verdict_date_ad",
            )
        )

        # Bucketed by exact changed-column set so each write touches only the
        # columns it derived — a concurrent scraper write on an untouched column
        # cannot be clobbered by our stale read.
        changed: dict[tuple[str, ...], list[CourtCase]] = {}
        for case in cases:
            stats["scanned"] += 1
            rows = hearings.get(case.case_number) or []
            updates, outcome = compute_promotion(case, rows)
            if not updates:
                # No disposition at all (a terminal-looking status whose
                # decision_type is NULL or outside the vocabulary) vs. already
                # correct. Only the former is a coverage gap worth reporting.
                if outcome is None:
                    stats["no_classifiable_outcome"] += 1
                continue

            stats["rows_changed"] += 1
            if "case_status" in updates:
                stats["case_status_fixed"] += 1
            if "verdict_type" in updates:
                stats["verdict_type_set"] += 1
            if "verdict_date_bs" in updates:
                stats["verdict_date_set"] += 1
            for key, value in updates.items():
                setattr(case, key, value)
            case.updated_at = timezone.now()
            changed.setdefault(tuple(sorted(updates)), []).append(case)

        if execute and changed:
            for group_fields, group in changed.items():
                CourtCase.objects.using(using).bulk_update(
                    group, [*group_fields, "updated_at"], batch_size=_WRITE_BATCH_SIZE,
                )

        if on_batch is not None:
            on_batch(stats)

    return stats


class Command(BaseCommand):
    help = (
        "Promote a decisive hearing's verdict onto its case row, for cases decided "
        "after their one-shot enrichment (dry-run by default)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--court", default="special",
                            help="court_identifier to scan (default: special).")
        parser.add_argument("--case", action="append", dest="cases", default=None,
                            help="limit to these case numbers (repeatable); for smoke tests.")
        parser.add_argument("--batch-size", type=int, default=500,
                            help="cases per page (default 500).")
        parser.add_argument("--limit", type=int, default=None,
                            help="stop after N candidate cases (smoke tests).")
        parser.add_argument("--execute", action="store_true",
                            help="apply changes (default: dry-run, no writes).")
        parser.add_argument("--i-understand-this-writes-prod", action="store_true",
                            dest="confirm",
                            help="required second flag to actually write.")

    def handle(self, *args, **o):
        execute = o["execute"]
        if execute and not o["confirm"]:
            raise CommandError(
                "Refusing to write: pass --i-understand-this-writes-prod "
                "alongside --execute."
            )

        mode = "APPLIED" if execute else "DRY-RUN (no writes)"
        court = o["court"]
        self.stdout.write(
            f"promote_hearing_verdicts [{mode}] db={NGM_DB} court={court}"
        )

        printed = [0]

        def _progress(stats):
            if stats["scanned"] - printed[0] >= 2_000:
                printed[0] = stats["scanned"]
                self.stdout.write(
                    f"  … scanned={stats['scanned']:,} changed={stats['rows_changed']:,}"
                )

        stats = run_promotion(
            court=court, only=o["cases"], batch_size=o["batch_size"],
            limit=o["limit"], execute=execute, on_batch=_progress,
        )

        self.stdout.write(self.style.SUCCESS(f"\n=== promote_hearing_verdicts {mode} ==="))
        for key in (
            "candidates", "scanned", "rows_changed", "case_status_fixed",
            "verdict_type_set", "verdict_date_set", "no_classifiable_outcome",
        ):
            self.stdout.write(f"  {key:24s}: {stats[key]:,}")
        if execute and stats["rows_changed"]:
            self.stdout.write(
                "\nbulk_update bypasses post_save: run "
                "`reindex_courtcases --since <today>` to re-index the changed rows."
            )
