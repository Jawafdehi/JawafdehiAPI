"""Select the next binding batch -- the master CSV *proposes*, production *decides*.

The batch-1 slice (`build_batch1_v2.py`: year + state + bigo off the CSV) only
works while nothing is bound yet. From batch 2 on, "which cases still need
binding" is a LIVE question: a case bound in batch 1 must drop out automatically,
with zero extra bookkeeping. This driver answers it against the control plane:

    master CSV row  (proposes slug + candidate material IRIs)
      -> year scope        (cheap: court_case_no[:3])
      -> optional --drop    (quarantine, e.g. a contradicted-match tier column)
      -> sort by bigo DESC  (highest-stakes first; read live in that order)
      -> LIVE get case
           state != DRAFT             -> skip (state is the truth, not the CSV)
           missing_candidates == []   -> skip (already fully bound: the "ledger")
      -> keep, until --limit reached

Boundness comes from the LIVE case via the SAME ``missing_candidates`` predicate
the binder uses (``bind_materials``), so selection and binding cannot drift on
what "already bound" means -- and, crucially, a case that merely has *some*
unrelated evidence is NOT skipped if it still lacks one of its candidates. This
is READ-ONLY: it performs GETs only and writes a batch CSV that
``bind_materials.py --batch-csv`` then consumes. It never PATCHes anything.

    uv run python -m casework.select_batch --master-csv master.csv --out batch2.csv \
        --year 078 --year 079 --limit 50
"""
import argparse
import csv
import os
import sys

from casework.bind_materials import candidates_from_row, missing_candidates
from casework.common.api import CaseworkApi

# Columns copied through to the batch CSV so the binder can read them. The
# material-IRI columns are exactly the `*_iri` names bind_materials already
# recognises (DEFAULT_MATERIAL_COLUMNS), so no renaming is needed.
OUTPUT_COLUMNS = (
    "slug", "court_case_no", "state", "bigo_npr",
    "press_release_iri", "court_order_iri", "abhiyog_ag_iri",
)
REQUIRED_STATE = "DRAFT"


def parse_bigo(row):
    """`bigo_npr` as a float for sorting; missing/garbage -> 0.0 (sorts last)."""
    try:
        return float(str(row.get("bigo_npr") or "").replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def in_year_scope(court_case_no, years):
    """First three chars of the case number (the fiscal year) in ``years``.

    Empty ``years`` means "no year filter" -- every row is in scope.
    """
    if not years:
        return True
    return (court_case_no or "")[:3] in set(years)


def _dropped_by(row, drops):
    """True if any ``COL=VALUE`` in ``drops`` matches this row (quarantine).

    A missing column never matches -- so a `--drop` naming a column the CSV
    lacks is a no-op, not a crash or a silent drop-everything.
    """
    return any((row.get(col) or "").strip() == val for col, val in drops)


def select_batch(rows, api, *, years=(), limit=0, drops=(), get_case=None):
    """Return the selected rows (bigo-desc), each still needing a live bind.

    ``get_case`` defaults to ``api.get_case_with_etag`` but is injectable for
    tests. ``limit<=0`` means "no cap". Returns ``(selected, stats)`` where
    ``stats`` counts every drop reason for the run summary.
    """
    get_case = get_case or api.get_case_with_etag
    stats = {"proposed": 0, "out_of_year": 0, "dropped": 0, "no_candidates": 0,
             "fetch_failed": 0, "not_draft": 0, "already_bound": 0, "selected": 0}

    # Cheap CSV-only pre-filters first; sort the survivors by stakes so the
    # live reads happen highest-bigo-first and we can stop at --limit.
    pool = []
    for row in rows:
        stats["proposed"] += 1
        if not in_year_scope(row.get("court_case_no"), years):
            stats["out_of_year"] += 1
            continue
        if _dropped_by(row, drops):
            stats["dropped"] += 1
            continue
        if not (row.get("slug") or "").strip() or not candidates_from_row(row):
            stats["no_candidates"] += 1
            continue
        pool.append(row)
    pool.sort(key=parse_bigo, reverse=True)

    selected = []
    for row in pool:
        if limit and len(selected) >= limit:
            break
        try:
            case, _etag = get_case(row["slug"])
        except Exception:  # noqa: BLE001 -- one unreadable case must not sink selection
            stats["fetch_failed"] += 1
            continue
        if case.get("state") != REQUIRED_STATE:
            stats["not_draft"] += 1
            continue
        if not missing_candidates(case, candidates_from_row(row)):
            stats["already_bound"] += 1   # live "ledger": nothing left to bind
            continue
        selected.append(row)
        stats["selected"] += 1
    return selected, stats


def write_batch(selected, path, columns=OUTPUT_COLUMNS):
    """Write the selected rows as a binder-ready CSV (only ``columns``)."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(columns), extrasaction="ignore")
        w.writeheader()
        for row in selected:
            w.writerow({c: row.get(c, "") for c in columns})
    return len(selected)


def _parse_drop(spec):
    """`COL=VALUE` -> `(COL, VALUE)`; raises on a missing `=`."""
    if "=" not in spec:
        raise argparse.ArgumentTypeError(f"--drop expects COL=VALUE, got {spec!r}")
    col, _, val = spec.partition("=")
    return (col.strip(), val.strip())


def build_parser():
    p = argparse.ArgumentParser(
        description="Select the next binding batch (live-derived; read-only).")
    p.add_argument("--master-csv", required=True,
                   help="Master CSV proposing slug + candidate material IRIs.")
    p.add_argument("--out", required=True, help="Batch CSV to write for the binder.")
    p.add_argument("--year", action="append", default=[], dest="years",
                   help="Fiscal-year scope, e.g. --year 078 --year 079 (repeatable).")
    p.add_argument("--limit", type=int, default=0, help="Max cases (0 = no cap).")
    p.add_argument("--drop", action="append", default=[], type=_parse_drop,
                   help="Quarantine rows where COL=VALUE (repeatable), e.g. "
                        "--drop match_tier=D_CONTRADICTED.")
    p.add_argument("--api-base-url", default=os.environ.get("JAWAFDEHI_API_BASE"),
                   help="Base URL of the case API; defaults to $JAWAFDEHI_API_BASE.")
    p.add_argument("--api-token", default="")
    return p


def run(args, api=None, rows=None):
    """Load rows, select live, write the batch CSV, return ``(selected, stats)``."""
    api = api or CaseworkApi(base_url=args.api_base_url, token=args.api_token or None)
    if rows is None:
        with open(args.master_csv, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    selected, stats = select_batch(rows, api, years=args.years, limit=args.limit,
                                   drops=args.drop)
    write_batch(selected, args.out)
    return selected, stats


def main(argv=None):
    args = build_parser().parse_args(argv)
    selected, stats = run(args)
    print(f"\n=== select batch (READ-ONLY) -> {args.out} ===")
    for key in ("proposed", "out_of_year", "dropped", "no_candidates",
                "fetch_failed", "not_draft", "already_bound", "selected"):
        print(f"  {key}: {stats[key]}")
    print(f"\nwrote {len(selected)} case(s). Next: "
          f"uv run python -m casework.bind_materials --batch-csv {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
