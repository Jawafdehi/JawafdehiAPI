"""One-time normalization of ``case_status`` on the existing court_cases corpus.

Applies the SAME shared parser the crawler now uses at write time
(:mod:`courts.case_status`) to the ~1.6M already-loaded ``ngm`` rows, so history
and freshly-scraped data are normalised identically. Fixes on existing data:

- DQ-01: clears the ~103k rows whose ``case_status`` is the scraped column header
  (``आदेश /फैसलाको किसिम``) — set to NULL;
- DQ-02: sets ``verdict_type`` from the arrow-form outcome enum, else from the
  final decisive hearing (was populated on only 3 of 1.6M rows);
- DQ-03: fills ``verdict_date_bs``/``verdict_date_ad`` from the paren form
  (~465k rows), never overwriting a value that is already set.

Divergence from the retired ngm script it replaces: that script stashed the
parsed lifecycle into ``extra_data['parsed_status']`` because the typed columns
did not exist yet. In the monorepo those columns are first-class (migration
0003), so this writes them directly and touches nothing else — the run stays
idempotent (a second pass changes zero rows) and never rewrites JSONB. The
``unmapped`` arrow-outcome count is reported in the tally, not persisted.

This is the deliberate exception to the importer contract: the importer never
writes ``case_status``/``verdict_*`` (scraper-owned); this command does, once.

SAFETY: dry-run by default. It never writes unless BOTH ``--execute`` and
``--i-understand-this-writes-prod`` are passed. Prints a projection tally.

    manage.py backfill_case_status --limit 5000            # dry-run projection
    manage.py backfill_case_status --execute --i-understand-this-writes-prod
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Q
from django.utils import timezone

from courts import case_status as cs
from courts.models import CourtCase

NGM_DB = "ngm"

# ``bulk_update`` sub-batch size. Each sub-batch is one UPDATE whose body is a
# ``CASE`` per field with one ``WHEN`` per row; a modest size bounds the statement
# (and the per-row CASE scan on Postgres) while still collapsing thousands of
# round-trips into a handful — the whole point of this command over the retired
# per-row ``.save()`` loop, which was ~14 rows/s against the ~38ms pod↔PG RTT.
# Django caps further if a batch would exceed the driver's bind-parameter budget.
_WRITE_BATCH_SIZE = 500

# Columns loaded per row (composite PK + the scraper-owned typed columns this
# pass may touch, plus extra_data for the hearing-based verdict fallback).
_LOAD = (
    "case_number", "court", "case_status", "verdict_type",
    "verdict_date_bs", "verdict_date_ad", "extra_data",
)


def compute_case_updates(case_status, verdict_type, verdict_date_bs, verdict_date_ad, hearings):
    """Pure transform: ``(column_updates, parsed)`` for one row. No DB access.

    ``column_updates`` holds only the columns whose value should change; an empty
    dict means the row is already normalised (idempotent no-op). ``parsed`` is the
    :class:`~courts.case_status.ParsedCaseStatus` (its ``unmapped`` flag feeds the
    DQ tally).
    """
    parsed = cs.parse_case_status(case_status)
    updates: dict[str, object] = {}

    # DQ-01 — a stored header/label artifact is not a real status.
    if cs.is_status_artifact(case_status):
        updates["case_status"] = None

    # DQ-02 — outcome enum from the status, else the final decisive hearing.
    new_verdict = parsed.verdict_type or cs.verdict_from_hearings(hearings)
    if new_verdict and new_verdict != verdict_type:
        updates["verdict_type"] = new_verdict

    # DQ-03 — fill a missing verdict date from the paren form; never overwrite.
    if parsed.verdict_date_bs and not verdict_date_bs:
        updates["verdict_date_bs"] = parsed.verdict_date_bs
        updates["verdict_date_ad"] = parsed.verdict_date_ad

    return updates, parsed


def _hearings_of(case: CourtCase):
    return (case.extra_data or {}).get("enrichment_hearings")


def run_backfill(*, batch_size=2000, limit=None, execute=False, using=NGM_DB, on_batch=None):
    """Iterate court_cases keyset-paginated, tally (and optionally apply) updates.

    Keyset pagination on the composite natural key ``(court, case_number)`` keeps
    memory bounded over 1.6M rows. In ``execute`` mode the changed rows of each
    page are written with a single
    :meth:`~django.db.models.query.QuerySet.bulk_update` (one UPDATE per
    ``_WRITE_BATCH_SIZE`` sub-batch, NOT one per row). That batching is the whole
    reason this command exists over a naive per-row ``.save()`` loop: at the
    ~38ms pod↔Postgres round-trip, per-row writes ran ~14 rows/s (≈27h for the
    corpus); the batched write is 20-50× that, so a re-backfill after a parser
    fix is practical.

    ``bulk_update`` emits ``UPDATE`` statements, which bypass ``post_save`` (and
    thus the live OpenSearch upsert and the auditlog ``LogEntry``) — so live
    indexing is intentionally skipped here. We bump ``updated_at`` on every
    changed row so a later ``reindex_courtcases --since`` re-indexes exactly the
    rows this backfill touched; the keyset is on ``(court, case_number)`` (never
    mutated) so it stays stable across writes.

    Rows are grouped by the columns that ACTUALLY changed and each group is
    written with only those columns (+ ``updated_at``), so the backfill never
    touches a column it did not derive — a concurrent scraper write on an
    untouched column can't be clobbered by our stale read, and DQ-03's "never
    clobber an existing verdict date" holds because an already-set date is simply
    not in the change-set. There are only a handful of distinct change-sets, so
    this stays a small constant number of statements per page, not one per row.
    Idempotent and re-runnable: extend the parser, re-run, and only
    newly-derivable rows change.
    """
    stats = {
        "scanned": 0,
        "rows_changed": 0,
        "header_cleared": 0,
        "verdict_type_set": 0,
        "verdict_date_set": 0,
        "unmapped": 0,
    }
    last_key = None
    while True:
        qs = (
            CourtCase.objects.using(using)
            .only(*_LOAD)
            .order_by("court", "case_number")
        )
        if last_key is not None:
            court, number = last_key
            qs = qs.filter(
                Q(court_id__gt=court)
                | (Q(court_id=court) & Q(case_number__gt=number))
            )
        rows = list(qs[:batch_size])
        if not rows:
            break

        # Rows bucketed by their exact changed-column set (e.g. ("case_status",)
        # vs ("verdict_date_ad", "verdict_date_bs")); each bucket is one or more
        # batched UPDATEs writing only those columns + updated_at.
        changed: dict[tuple[str, ...], list[CourtCase]] = {}
        for case in rows:
            stats["scanned"] += 1
            updates, parsed = compute_case_updates(
                case.case_status, case.verdict_type,
                case.verdict_date_bs, case.verdict_date_ad, _hearings_of(case),
            )
            if parsed.unmapped:
                stats["unmapped"] += 1
            if not updates:
                continue

            stats["rows_changed"] += 1
            if "case_status" in updates:
                stats["header_cleared"] += 1
            if "verdict_type" in updates:
                stats["verdict_type_set"] += 1
            if "verdict_date_bs" in updates:
                stats["verdict_date_set"] += 1
            for key, value in updates.items():
                setattr(case, key, value)
            case.updated_at = timezone.now()
            changed.setdefault(tuple(sorted(updates)), []).append(case)

        if execute and changed:
            # bulk_update is atomic per call. Grouping by changed-column set keeps
            # the write surface minimal (only derived columns are written) while
            # still collapsing each group into batched UPDATEs (~a handful of
            # distinct sets → a handful of statements, never one per row).
            for group_fields, cases in changed.items():
                CourtCase.objects.using(using).bulk_update(
                    cases, [*group_fields, "updated_at"], batch_size=_WRITE_BATCH_SIZE,
                )

        last_key = (rows[-1].court_id, rows[-1].case_number)
        if on_batch is not None:
            on_batch(stats)
        if limit and stats["scanned"] >= limit:
            break

    return stats


class Command(BaseCommand):
    help = (
        "One-time normalization of existing court_cases case_status into the typed "
        "verdict columns (dry-run by default; DQ-01/02/03)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=2000,
                            help="rows per keyset page (SELECT window; default 2000).")
        parser.add_argument("--limit", type=int, default=None,
                            help="stop after N rows (smoke tests).")
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
        self.stdout.write(f"backfill_case_status [{mode}] db={NGM_DB}")

        printed = [0]

        def _progress(stats):
            if stats["scanned"] - printed[0] >= 50_000:
                printed[0] = stats["scanned"]
                self.stdout.write(
                    f"  … scanned={stats['scanned']:,} changed={stats['rows_changed']:,}"
                )

        stats = run_backfill(
            batch_size=o["batch_size"], limit=o["limit"],
            execute=execute, on_batch=_progress,
        )

        self.stdout.write(self.style.SUCCESS(f"\n=== backfill_case_status {mode} ==="))
        for key in (
            "scanned", "rows_changed", "header_cleared",
            "verdict_type_set", "verdict_date_set", "unmapped",
        ):
            self.stdout.write(f"  {key:16s}: {stats[key]:,}")
