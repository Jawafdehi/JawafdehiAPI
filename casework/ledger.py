#!/usr/bin/env python
"""Consolidate per-run enricher event logs into a current-state ledger.

Each enricher run appends JSONL events (casework/common/cli.py::log_event) to
``work/enricher-runs/<ts>-<stage>-<runid>.events.jsonl`` -- one row per step per
case: ``{ts, run_id, stage, slug, step, status, detail, elapsed_ms}``. This
module folds ALL of those into a ledger: the LATEST decisive outcome per
``(slug, stage)``, so a re-run supersedes an earlier one.

READ-ONLY over the logs. It never calls the API and never re-runs an enricher;
the event logs are the record of what the pipeline actually did. Its value over
the live per-field idempotency check (which already stops batch 2 re-touching a
populated case) is that it distinguishes *we enriched it* from *it was already
populated*, and gives an auditable "what did we change, when" across batches.

    python casework/ledger.py                       # summary + write ledger
    python casework/ledger.py --stage timeline      # summary for one stage
    python casework/ledger.py --status error        # list the failed cases
    python casework/ledger.py --no-write            # print only, write nothing

The written ledger is always the FULL consolidated set; --stage/--slug/--status
filter only what is printed.
"""
import argparse
import collections
import glob
import json
import os
from pathlib import Path

from casework.common.cli import _DEFAULT_LOG_DIR, _REPO_ROOT

# A case-stage OUTCOME is any status a RunReport records as its per-case verdict
# (see casework/common/pipeline.py::RunReport). The vocabulary is open and
# stage-specific -- beyond the five core verdicts there are `would-enrich` /
# `would-convert` (dry-run), convert's `converted` / `failed`, and `llm-error`.
# Rather than whitelist them (a too-narrow list silently DROPS a stage's real
# result -- e.g. every convert outcome), we treat everything as an outcome
# EXCEPT the known intermediate step signals below. A ledger must fail toward
# inclusion, not omission.
#
# `planned` is the one dry-run status that IS excluded: bind_materials maps a
# dry-run WOULD_PATCH to `planned` precisely so it stays out of the "what did we
# change, when" audit (bind_materials.py::_ledger_status). It is a non-outcome
# by design -- a bind dry run changed nothing -- so it belongs here, not with
# the `would-*` extraction previews that DO record a per-case verdict.
NON_OUTCOME_STATUSES = frozenset({"ok", "start", "fallback", "none", "planned"})

_DEFAULT_LEDGER = _REPO_ROOT / "work" / "enrichment-ledger.jsonl"


def _resolve_log_dir(explicit=None):
    """Mirror configure_run_logging's resolution: explicit > env > default."""
    return Path(explicit or os.environ.get("CASEWORK_RUN_LOG_DIR") or _DEFAULT_LOG_DIR)


def iter_events(log_dir):
    """Yield every event dict from ``*.events.jsonl`` under ``log_dir``.

    Tolerant: skips blank lines and a partially-written trailing line (a run
    killed mid-write) rather than raising, and reads files in name order (the
    filenames are timestamp-prefixed, so this is roughly chronological).
    """
    for path in sorted(glob.glob(os.path.join(str(log_dir), "*.events.jsonl"))):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue


def build_ledger(log_dir=None, *, non_outcome_statuses=NON_OUTCOME_STATUSES):
    """Fold events into ``{(slug, stage): latest-outcome-row}``.

    Latest by ``ts`` (ISO-8601 UTC sorts lexicographically); ties resolve to
    the later event encountered. Events whose status is an intermediate step
    signal (``non_outcome_statuses``) are skipped; every other status is treated
    as a case-stage outcome. A case that never reached an outcome (e.g. the run
    crashed after "start") is absent.
    """
    non_outcomes = set(non_outcome_statuses)
    ledger: dict = {}
    for ev in iter_events(_resolve_log_dir(log_dir)):
        status = ev.get("status")
        # A status-less event is not an outcome. Skip it rather than index
        # ev["status"] below -- iter_events is deliberately tolerant of
        # malformed rows (blank/partial lines), and one well-formed JSON line
        # that simply lacks "status" must not KeyError and sink the whole build.
        if not status or status in non_outcomes:
            continue
        slug, stage = ev.get("slug"), ev.get("stage")
        if not slug or not stage:
            continue
        key = (slug, stage)
        prev = ledger.get(key)
        ts = ev.get("ts") or ""
        if prev is None or ts >= (prev.get("ts") or ""):
            ledger[key] = {
                "slug": slug,
                "stage": stage,
                "status": status,
                "ts": ev.get("ts"),
                "run_id": ev.get("run_id"),
                "detail": ev.get("detail", ""),
            }
    return ledger


def stage_summary(ledger):
    """Per-stage ``Counter`` of outcome statuses over a built ledger."""
    out: dict = collections.defaultdict(collections.Counter)
    for row in ledger.values():
        out[row["stage"]][row["status"]] += 1
    return out


def write_ledger(ledger, path):
    """Write the consolidated ledger as JSONL, sorted by (stage, slug).

    ``ensure_ascii=False`` so Devanagari details survive. Returns the row count.
    """
    rows = sorted(ledger.values(), key=lambda r: (r["stage"], r["slug"]))
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(rows)


def _print_summary(ledger, *, stages=None):
    summ = stage_summary(ledger)
    keep = set(stages) if stages else None
    for stage in sorted(summ):
        if keep and stage not in keep:
            continue
        counts = summ[stage]
        parts = "  ".join(f"{status}={counts[status]}" for status in sorted(counts))
        print(f"{stage:14} {parts}  (total {sum(counts.values())})")


def _print_rows(ledger, *, stages=None, slugs=None, statuses=None):
    keep_stage = set(stages) if stages else None
    keep_slug = set(slugs) if slugs else None
    keep_status = set(statuses) if statuses else None
    rows = sorted(ledger.values(), key=lambda r: (r["stage"], r["slug"]))
    print()
    for r in rows:
        if keep_stage and r["stage"] not in keep_stage:
            continue
        if keep_slug and r["slug"] not in keep_slug:
            continue
        if keep_status and r["status"] not in keep_status:
            continue
        detail = f"  {r['detail']}" if r["detail"] else ""
        print(f"  {r['status']:9} {r['stage']:12} {r['slug']}{detail}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Consolidate enricher event logs into a current-state ledger.")
    ap.add_argument("--log-dir", default=None,
                    help="dir of *.events.jsonl (default: CASEWORK_RUN_LOG_DIR "
                         "or work/enricher-runs)")
    ap.add_argument("--out", default=None,
                    help=f"write consolidated ledger here (default {_DEFAULT_LEDGER})")
    ap.add_argument("--no-write", action="store_true",
                    help="print only; do not write the ledger file")
    ap.add_argument("--stage", action="append",
                    help="restrict printed output to these stage(s)")
    ap.add_argument("--slug", action="append",
                    help="list rows for these slug(s)")
    ap.add_argument("--status", action="append",
                    help="list rows with these status(es), e.g. --status error")
    args = ap.parse_args(argv)

    ledger = build_ledger(_resolve_log_dir(args.log_dir))

    if not args.no_write:
        out = Path(args.out) if args.out else _DEFAULT_LEDGER
        out.parent.mkdir(parents=True, exist_ok=True)
        n = write_ledger(ledger, out)
        print(f"wrote {n} case-stage rows -> {out}\n")

    _print_summary(ledger, stages=args.stage)
    if args.slug or args.status:
        _print_rows(ledger, stages=args.stage, slugs=args.slug, statuses=args.status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
