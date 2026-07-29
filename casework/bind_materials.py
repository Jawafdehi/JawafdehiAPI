"""Bind existing lake materials to a case's ``evidence[]`` (the join-not-source step).

Most CIAA supporting documents already exist in the materials lake -- press
releases (100% present), court orders (~86%), and the sha256-resolved AG
abhiyog patras. They are simply not *attached* to their case. This CLI performs
that attachment safely, composing the primitives in ``common/``:

    GET case + ETag                     (api.get_case_with_etag)
      -> GUARD state == DRAFT           (else SKIP; never touch PUBLISHED/IN_REVIEW)
      -> VERIFY each candidate exists    (materials.material_exists: 200/absent/uncertain)
           absent -> drop + flag; uncertain -> ABORT the case (never a partial write)
      -> MERGE current + verified new    (dedupe by IRI, preserve existing order)
      -> NO-OP check                     (merged == current -> skip the write)
      -> PATCH /evidence with If-Match   (api.replace_list, conditional on the ETag)

Why the paranoia: the whole-list replace is DESTRUCTIVE -- the server deletes
every existing evidence row and recreates from exactly what we send, and it
validates IRI *grammar* only (never material existence). So a partial or
typo'd list silently destroys data. Hence: verify-before-bind, merge the FULL
list, abort on any uncertainty, and gate the write on If-Match so a concurrent
edit 412s instead of being clobbered.

Dry-run is the DEFAULT (prints the exact PATCH it WOULD send). ``--apply``
opts into writing, and writing to any non-loopback host additionally requires
``--allow-remote-writes`` (enforced in CaseworkApi, not here).

Usage:
    uv run python -m casework.bind_materials --batch-csv batch2.csv --dry-run
    uv run python -m casework.bind_materials --batch-csv batch2.csv --apply
"""
import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass, field

from casework.common.api import CaseworkApi
from casework.common.cli import (
    _utc_iso_now, add_common_args, basic_auth_from_env, configure_run_logging,
    log_run_footer, log_run_header, print_summary, resolve_api_token,
)
from casework.common.materials import material_iri, probe_material

STAGE = "bind"
REQUIRED_STATE = "DRAFT"
EVIDENCE_PATH = "evidence"

# Batch-CSV columns that carry candidate material IRIs. Each cell may hold
# several " ; "-separated tokens, each optionally suffixed with " [status]".
DEFAULT_MATERIAL_COLUMNS = (
    "press_release_material", "court_order_material", "abhiyog_ag_material",
    "press_release_iri", "court_order_iri", "abhiyog_ag_iri",
)


# ---------------------------------------------------------------------------
# Candidate resolution -- turn IRIs (from a batch CSV) into (source, ident).
# ---------------------------------------------------------------------------


def parse_source_ident(iri):
    """``https://.../material/<source>/<ident>`` -> ``(source, ident)`` | ``None``.

    Court-order idents are lowercased here because an UPPERCASE ident returns
    HTTP 400 -- so this is the one place normalization must not be skipped.
    """
    if not iri or "/material/" not in iri:
        return None
    tail = iri.split("/material/", 1)[1].strip().rstrip("/")
    source, _, ident = tail.partition("/")
    if not source or not ident:
        return None
    if source == "court_order":
        ident = ident.lower()
    return (source, ident)


def candidates_from_row(row, columns=DEFAULT_MATERIAL_COLUMNS):
    """Extract an ordered, de-duplicated list of ``(source, ident)`` from a row.

    Re-derives from the IRI text rather than trusting any status column, and
    preserves first-seen order so the resulting bind order is deterministic.
    """
    out, seen = [], set()
    for col in columns:
        for tok in (row.get(col) or "").split(" ; "):
            tok = tok.split(" [")[0].strip()
            si = parse_source_ident(tok)
            if si and si not in seen:
                seen.add(si)
                out.append(si)
    return out


# ---------------------------------------------------------------------------
# The pure planning core -- no I/O except the existence probe (injected `api`).
# ---------------------------------------------------------------------------


@dataclass
class BindPlan:
    slug: str
    action: str  # WOULD_PATCH | NOOP | SKIP_STATE | ABORT_UNCERTAIN
    state: str = ""
    if_match: str | None = None
    n_current: int = 0
    added: list = field(default_factory=list)      # IRIs that would be bound
    dropped: list = field(default_factory=list)    # IRIs that probed ABSENT
    uncertain: list = field(default_factory=list)  # IRIs that probed UNCERTAIN
    patch_items: list = field(default_factory=list)  # the FULL merged evidence list
    probes: list = field(default_factory=list)     # ProbeResult per candidate probed
    reason: str = ""

    @property
    def patch_body(self):
        return [{"op": "replace", "path": f"/{EVIDENCE_PATH}", "value": self.patch_items}]

    @property
    def n_merged(self):
        return len(self.patch_items) if self.action == "WOULD_PATCH" else self.n_current


def current_evidence(case):
    """Normalize the case's evidence into the {material_iri, additional_details}
    shape the PATCH expects, preserving order."""
    return [
        {"material_iri": e.get("material_iri"),
         "additional_details": e.get("additional_details") or ""}
        for e in (case.get("evidence") or [])
        if e.get("material_iri")
    ]


def merge_evidence(current, add_iris):
    """Append each new IRI not already present, preserving existing order and
    de-duplicating. Never reorders or drops an existing entry -- the whole-list
    replace makes any omission destructive."""
    have = {e["material_iri"] for e in current}
    merged = list(current)
    for iri in add_iris:
        if iri not in have:
            merged.append({"material_iri": iri, "additional_details": ""})
            have.add(iri)
    return merged


def missing_candidates(case, candidates):
    """Return the ``(source, ident)`` candidates NOT already bound to ``case``.

    The shared definition of "still needs binding", so the selection ledger and
    the binder cannot drift. The ledger skips a case only when this is empty
    (fully bound already) -- NOT when the case merely has *some* evidence, which
    would wrongly skip a case that still needs, say, its press release. The
    binder computes the same set internally when it merges, so the two agree by
    construction. Note this is existence-in-the-list only; it does not probe
    whether the material actually exists -- that is the binder's job.
    """
    have = {e["material_iri"] for e in current_evidence(case)}
    return [(s, i) for (s, i) in candidates if material_iri(s, i) not in have]


def plan_case(api, case, etag, candidates, required_state=REQUIRED_STATE):
    """Build a BindPlan for one case. Probes each candidate via ``material_exists``.

    Guarantees: never plans a write for a non-DRAFT case; never includes an
    absent or uncertain material; aborts the whole case if ANY candidate is
    uncertain (a partial list would destroy data); emits NOOP when the merge
    changes nothing so a re-run is idempotent.
    """
    slug = case.get("slug")
    state = case.get("state")
    if state != required_state:
        return BindPlan(slug=slug, action="SKIP_STATE", state=state,
                        reason=f"state {state!r} != {required_state!r}")

    current = current_evidence(case)
    have = {e["material_iri"] for e in current}
    add, dropped, uncertain, probes = [], [], [], []
    for source, ident in candidates:
        iri = material_iri(source, ident)
        if iri in have:
            continue  # already bound -> contributes nothing
        pr = probe_material(api, source, ident)
        probes.append(pr)
        if pr.verdict is True:
            if iri not in add:
                add.append(iri)
        elif pr.verdict is False:
            dropped.append(iri)
        else:
            uncertain.append(iri)

    if uncertain:
        # Refuse to write a partial list on uncertainty -- the replace is
        # destructive, so "some but not all" would drop real binds.
        return BindPlan(slug=slug, action="ABORT_UNCERTAIN", state=state,
                        if_match=etag, n_current=len(current),
                        dropped=dropped, uncertain=uncertain, probes=probes,
                        reason=f"{len(uncertain)} material(s) uncertain")

    merged = merge_evidence(current, add)
    if merged == current:
        return BindPlan(slug=slug, action="NOOP", state=state, if_match=etag,
                        n_current=len(current), dropped=dropped, probes=probes)
    return BindPlan(slug=slug, action="WOULD_PATCH", state=state, if_match=etag,
                    n_current=len(current), added=add, dropped=dropped,
                    patch_items=merged, probes=probes)


def apply_plan(api, plan):
    """Execute a WOULD_PATCH plan: whole-list replace of /evidence, conditional
    on the ETag captured at plan time (If-Match). A 412 means the case changed
    since we read it -- the merge is stale; the caller should re-read and retry
    rather than force. Refuses to apply any non-WOULD_PATCH plan, and fails
    closed when no ETag was captured (see below)."""
    if plan.action != "WOULD_PATCH":
        raise ValueError(f"apply_plan called on a {plan.action} plan for {plan.slug!r}")
    # Fail closed on a missing ETag. get_case_with_etag() returns None when the
    # server sent no ETag; without one, If-Match is absent and replace_list()
    # becomes an UNCONDITIONAL whole-list replace -- the exact destructive,
    # concurrent-edit-clobbering write this module's optimistic-concurrency
    # design exists to prevent. Refuse rather than write unguarded (consistent
    # with ABORT_UNCERTAIN: never an unguarded destructive write). run() records
    # the raise as APPLY_FAILED, so one such case does not sink the batch.
    if not plan.if_match:
        raise RuntimeError(
            f"refusing unconditional whole-list evidence replace for {plan.slug!r}: "
            "no ETag was captured at read time, so a concurrent edit cannot be "
            "detected (If-Match would be absent) and the destructive replace could "
            "silently clobber it -- investigate why the case GET returned no ETag")
    return api.replace_list(plan.slug, EVIDENCE_PATH, plan.patch_items,
                            if_match=plan.if_match)


# ---------------------------------------------------------------------------
# CLI shell -- selection + logging + optional apply around the pure core.
# ---------------------------------------------------------------------------


def _build_api(args):
    """Construct the client. Bearer when a token is given, else local DEV_AUTH
    Basic (CASEWORK_API_USER/PASSWORD) -- the same wiring convert.py and every
    enricher use. Without this Basic branch a bare loopback bind (this tool's
    primary use) could not authenticate: a local DEV_AUTH server rejects Bearer
    (it routes to OIDC), so token-only would raise "exactly one of token/basic"
    on every local run.

    The token itself comes from `resolve_api_token` ($JAWAFDEHI_API_TOKEN, or
    the discouraged `--api-token` flag, which warns), not from `args` directly
    -- a token on argv is readable by any local user via `ps -af`."""
    token = resolve_api_token(args)
    if token:
        return CaseworkApi(base_url=args.api_base_url, token=token,
                           allow_remote_writes=args.allow_remote_writes)
    return CaseworkApi(base_url=args.api_base_url, basic=basic_auth_from_env(),
                       allow_remote_writes=args.allow_remote_writes)


_VERDICT_LABEL = {True: "EXISTS", False: "ABSENT", None: "UNCERTAIN"}
_VERDICT_ACTION = {True: "bind", False: "dropped", None: "abort"}


def _source_of(iri):
    """`https://.../material/<source>/<ident>` -> `<source>` (best effort)."""
    try:
        return iri.split("/material/", 1)[1].split("/", 1)[0]
    except IndexError:
        return "?"


def _ledger_status(action, final):
    """Map a bind outcome onto the ledger's status vocabulary
    (``casework/ledger.py``) so bind runs consolidate alongside the enrichers.

    A dry-run ``WOULD_PATCH`` changed nothing, so it maps to ``"planned"`` --
    a status the ledger lists in ``NON_OUTCOME_STATUSES`` and therefore folds
    into no outcome. The ledger is a record of what we *changed*; only
    ``--apply`` (-> ``APPLIED``) is a real change. This keeps a dry run
    truthfully invisible in the "what did we change, when" audit.
    """
    if final == "APPLIED":
        return "enriched"
    if final in ("FETCH_FAILED", "APPLY_FAILED"):
        return "error"
    if action == "NOOP":
        return "already"
    if action == "SKIP_STATE":
        return "skipped"
    if action == "ABORT_UNCERTAIN":
        return "unmet"
    return "planned"  # dry-run WOULD_PATCH: planned, not (yet) a change


def _write_event(events_path, record):
    """Append one complete JSON event. Devanagari survives (ensure_ascii=False)."""
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run(args, api=None, rows=None):
    """Plan (and optionally apply) binds for every row. Returns (stats, plans).

    ``api``/``rows`` are injectable for testing; in normal use they come from
    ``_build_api(args)`` and the ``--batch-csv`` file. The human-readable log
    shows, per case: the court case number, one line per material (probe path,
    HTTP status, verdict, action), and a plan line (counts, evidence before->
    after, If-Match ETag, latency). The events JSONL carries the full record
    regardless. The run ends with by-source totals and a needs-attention list.
    """
    api = api or _build_api(args)
    if rows is None:
        with open(args.batch_csv, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))

    logger, run_id, paths = configure_run_logging(STAGE, verbose=args.verbose)
    log_run_header(logger, stage=STAGE, base_url=args.api_base_url,
                   dry_run=args.dry_run, provider="-", model="-",
                   n_selected=len(rows), run_id=run_id, paths=paths)

    started = time.monotonic()
    stats, plans, attention, by_source = {}, [], [], {}

    for row in rows:
        slug = row.get("slug") or ""
        cno = row.get("court_case_no") or "?"
        t0 = time.monotonic()
        try:
            case, etag = api.get_case_with_etag(slug)
        except Exception as exc:  # noqa: BLE001 -- one bad case must not sink the batch
            ms = int((time.monotonic() - t0) * 1000)
            stats["FETCH_FAILED"] = stats.get("FETCH_FAILED", 0) + 1
            logger.warning("[%s | %s] FETCH_FAILED  %s  (%dms)", cno, slug, str(exc)[:120], ms)
            attention.append(f"{cno} ({slug}): fetch failed -- {str(exc)[:100]}")
            _write_event(paths["events"], {"ts": _utc_iso_now(), "run_id": run_id,
                         "stage": STAGE, "slug": slug, "court_case_no": cno,
                         "action": "FETCH_FAILED", "final": "FETCH_FAILED",
                         "status": "error", "error": str(exc)[:200], "elapsed_ms": ms})
            continue

        try:
            plan = plan_case(api, case, etag, candidates_from_row(row))
        except Exception as exc:  # noqa: BLE001 -- one bad case must not sink the batch
            ms = int((time.monotonic() - t0) * 1000)
            stats["PLAN_FAILED"] = stats.get("PLAN_FAILED", 0) + 1
            logger.warning("[%s | %s] PLAN_FAILED  %s  (%dms)", cno, slug, str(exc)[:120], ms)
            attention.append(f"{cno} ({slug}): plan failed -- {str(exc)[:100]}")
            _write_event(paths["events"], {"ts": _utc_iso_now(), "run_id": run_id,
                         "stage": STAGE, "slug": slug, "court_case_no": cno,
                         "action": "PLAN_FAILED", "final": "PLAN_FAILED",
                         "status": "error", "error": str(exc)[:200], "elapsed_ms": ms})
            continue
        ms = int((time.monotonic() - t0) * 1000)
        plans.append(plan)

        # Case header + one line per probed material.
        logger.info("[%s | %s]", cno, slug)
        for pr in plan.probes:
            status = pr.status if pr.status is not None else "ERR"
            logger.info("    %-18s GET /api%s -> %s %s -> %s", pr.source, pr.path,
                        status, _VERDICT_LABEL[pr.verdict], _VERDICT_ACTION[pr.verdict])

        # Optional apply (only for a WOULD_PATCH plan, only with --apply).
        final = plan.action
        if plan.action == "WOULD_PATCH" and not args.dry_run:
            try:
                apply_plan(api, plan)
                final = "APPLIED"
            except Exception as exc:  # noqa: BLE001
                final = "APPLY_FAILED"
                attention.append(f"{cno} ({slug}): apply failed -- {str(exc)[:100]}")

        # Plan/summary line per case.
        if plan.action == "WOULD_PATCH":
            logger.log(
                30 if final == "APPLY_FAILED" else 20,
                "    %s  +%d bound  %d absent  If-Match %s  (%dms)",
                final, len(plan.added), len(plan.dropped), plan.if_match, ms)
            if final != "APPLY_FAILED":
                for iri in plan.added:
                    by_source[_source_of(iri)] = by_source.get(_source_of(iri), 0) + 1
        elif plan.action == "NOOP":
            logger.info("    NOOP (already complete)  %d absent  (%dms)", len(plan.dropped), ms)
        elif plan.action == "SKIP_STATE":
            logger.info("    SKIP_STATE  state=%s != %s", plan.state, REQUIRED_STATE)
        elif plan.action == "ABORT_UNCERTAIN":
            logger.warning("    ABORT_UNCERTAIN  %d uncertain -- refusing partial write  (%dms)",
                           len(plan.uncertain), ms)

        # Collect anything a human should look at.
        if plan.dropped:
            attention.append(f"{cno} ({slug}): dropped absent -> {', '.join(plan.dropped)}")
        if plan.action == "ABORT_UNCERTAIN":
            attention.append(f"{cno} ({slug}): ABORTED on uncertain -> {', '.join(plan.uncertain)}")

        stats[final] = stats.get(final, 0) + 1
        _write_event(paths["events"], {
            "ts": _utc_iso_now(), "run_id": run_id, "stage": STAGE, "slug": slug,
            "court_case_no": cno, "status": _ledger_status(plan.action, final),
            "action": plan.action, "final": final, "state": plan.state,
            "if_match": plan.if_match, "n_current": plan.n_current, "n_merged": plan.n_merged,
            "added": plan.added, "dropped": plan.dropped, "uncertain": plan.uncertain,
            "probes": [{"source": p.source, "ident": p.ident, "path": p.path,
                        "status": p.status, "verdict": p.verdict} for p in plan.probes],
            "elapsed_ms": ms,
        })

    if getattr(args, "report", None):
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump({"dry_run": args.dry_run, "run_id": run_id,
                       "plans": [_plan_to_dict(p) for p in plans]},
                      f, ensure_ascii=False, indent=1)

    duration = time.monotonic() - started

    # By-source totals: how many of each material type would bind.
    if by_source:
        logger.info("by source: %s",
                    ", ".join(f"{k}={v}" for k, v in sorted(by_source.items())))
    # Needs-attention: every drop, abort, or fetch/apply failure, collected.
    if attention:
        logger.warning("NEEDS ATTENTION (%d):", len(attention))
        for line in attention:
            logger.warning("   - %s", line)
    else:
        logger.info("needs attention: none")

    log_run_footer(logger, stage=STAGE, stats=stats, duration_s=duration)
    return stats, plans


def _plan_to_dict(p):
    return {"slug": p.slug, "action": p.action, "state": p.state,
            "if_match": p.if_match, "n_current": p.n_current, "n_merged": p.n_merged,
            "added": p.added, "dropped": p.dropped, "uncertain": p.uncertain,
            "probes": [{"source": pr.source, "ident": pr.ident, "path": pr.path,
                        "status": pr.status, "verdict": pr.verdict} for pr in p.probes],
            "patch_body": p.patch_body if p.action == "WOULD_PATCH" else None,
            "reason": p.reason}


def build_parser():
    parser = argparse.ArgumentParser(description="Bind lake materials to case evidence.")
    add_common_args(parser, state_flag=False)
    parser.add_argument("--batch-csv", required=True,
                        help="CSV with a `slug` column and material-IRI columns.")
    parser.add_argument("--report", default="",
                        help="Optional path to write a JSON run report.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    stats, _ = run(args)
    print_summary(stats, args.dry_run, "bind materials")
    return 0


if __name__ == "__main__":
    sys.exit(main())
