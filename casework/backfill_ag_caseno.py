"""Backfill ``jawafdehi:caseNumber`` onto AG अभियोगपत्र materials in the lake.

Consumes the map produced by ``casework.source_abhiyog`` and restores the one
field the bulk scrape lost, so the indictments can be joined to their cases:

    GET /materials/ag/<id>/          (the CURRENT full document)
      -> GUARD identity + type       (recordId == id, officeLevel == 1,
                                      sourceType == AG_ABHIYOG_PATRA)
      -> GUARD existing value        (same -> SKIP idempotent;
                                      DIFFERENT -> QUARANTINE, never overwrite)
      -> MERGE onto the full doc     (add caseNumber + provenance only)
      -> PUT the COMPLETE document   (api.put_material)

Why a read-modify-write rather than a patch: the materials endpoint has NO
partial-update verb. ``PATCH`` there sets only ``visibility_policy``, and
``PUT`` funnels into ``upsert_single_source_material``, which replaces the
``data`` column WHOLESALE. Sending only the new field would therefore destroy
``text`` and ``associatedMedia``. So the current document is fetched, the field
is added in memory, and the whole document goes back.

Concurrency caveat: unlike the case endpoint, the materials endpoint exposes no
ETag/If-Match, so this read-modify-write cannot be made conditional. A
concurrent writer's change between our GET and PUT would be lost. Two defences,
one enforced and one not:

  * against ITSELF -- enforced. An advisory single-instance lock (``--lock-file``,
    see :func:`single_instance`) refuses to start while another run holds it.
    This is not hypothetical: three stray concurrent processes from earlier
    launches tripled the request rate during the recovery dry-run and produced a
    persistent spray of 429s that read as lake errors until they were found.
  * against an AG RE-INGEST -- not enforced. That writer is a different process
    on a different schedule, so this remains an operational constraint: do not
    run the two together.

Dry-run is the DEFAULT (logs the exact PUT it WOULD send, writes nothing).
``--apply`` opts into writing, and writing to any non-loopback host
additionally requires ``--allow-remote-writes`` (enforced in CaseworkApi).

Usage:
    uv run python -m casework.backfill_ag_caseno --map work/ag-caseno-map.json
    uv run python -m casework.backfill_ag_caseno --map ... --apply --allow-remote-writes
"""
import argparse
import contextlib
import copy
import json
import os
import sys
import time
import urllib.error
from pathlib import Path

from casework.common.api import CaseworkApi
from casework.common.cli import (
    add_common_args, basic_auth_from_env, configure_run_logging, log_event,
    nonneg_float, nonneg_int, print_summary,
)
from casework.common.materials import ag_ident, material_path

STAGE = "backfill_ag_caseno"
AG_SOURCE = "ag"
MATERIAL_TYPE = "charge_sheet"
EXPECTED_SOURCE_TYPE = "AG_ABHIYOG_PATRA"
SPECIAL_OFFICE_LEVEL = 1

#: The ``additionalType`` discriminator ``materials/jsonld.py`` maps to
#: ``charge_sheet``. Checked (not assumed) because the PUT sends
#: ``MATERIAL_TYPE`` explicitly: the server would otherwise infer the type from
#: this very field, and a bare ``DigitalDocument`` infers to ``document``. So a
#: doc whose discriminator CONTRADICTS charge_sheet must never be written --
#: sending our constant would silently retype it.
CHARGE_SHEET_ADDITIONAL_TYPE = "jawafdehi:ChargeSheet"

#: Identity fields the guard needs. NOTE: `materials/sourcing/ag/shaper.py` does
#: NOT emit either of these -- they were observed only on the production docs,
#: which means the rows in the lake were written by an ingest path that has since
#: diverged from the in-repo shaper. Their ABSENCE is therefore reported as
#: GUARD_MISSING, distinct from GUARD_FAIL: an all-rows GUARD_MISSING run means
#: the ingest path changed, NOT that the recovered map is wrong.
_IDENTITY_KEYS = ("jawafdehi:recordId", "jawafdehi:officeLevel")

MAX_BACKOFF_S = 30

#: Default advisory lock. Lives beside the run logs so it shares their lifetime
#: and is obvious to an operator looking for why a run refused to start.
DEFAULT_LOCK_FILE = Path(
    os.environ.get("CASEWORK_RUN_LOG_DIR")
    or Path(__file__).resolve().parents[1] / "work" / "enricher-runs"
) / f"{STAGE}.lock"

#: HTTP codes that will never succeed on a retry: the request or the caller's
#: credentials are wrong, not the server's mood. Retrying them burns the entire
#: backoff ladder on EVERY row (with the defaults, ~15s x N rows) before
#: surfacing what the very first response already said.
_FATAL_HTTP = (400, 401, 403, 405, 410, 501)

CASE_NO_KEY = "jawafdehi:caseNumber"
SOURCE_KEY = "jawafdehi:caseNumberSource"
AMBIGUOUS_KEY = "jawafdehi:caseNumberAmbiguous"
ALT_KEY = "jawafdehi:caseNumberAlt"

#: Outcomes. Only WOULD_APPLY/APPLIED mutate anything.
WOULD_APPLY = "WOULD_APPLY"
APPLIED = "APPLIED"
APPLY_FAILED = "APPLY_FAILED"
SKIP_IDEMPOTENT = "SKIP_IDEMPOTENT"
CONFLICT_QUARANTINE = "CONFLICT_QUARANTINE"
GUARD_FAIL = "GUARD_FAIL"
GUARD_MISSING = "GUARD_MISSING"
GONE = "GONE"
GET_ERROR = "GET_ERROR"
GET_FATAL = "GET_FATAL"

#: Outcomes that mean the run itself is broken (bad credentials, wrong host, a
#: dead API) rather than one odd record. A consecutive streak of these trips the
#: circuit breaker -- see :func:`run`.
_FAILURE_OUTCOMES = frozenset({GET_ERROR, GET_FATAL, APPLY_FAILED})

#: Required on every map row; the writer refuses a map that lacks them.
_REQUIRED_ROW_KEYS = ("id", "material_iri", "case_number", "source", "in_lake")


@contextlib.contextmanager
def single_instance(lock_path):
    """Refuse to start while another backfill run holds ``lock_path``.

    The GET->merge->PUT here cannot be made conditional (the materials endpoint
    exposes no ETag/If-Match), so overlapping runs can lose each other's writes.
    That constraint was documented but enforced only by convention -- and it has
    already bitten once: three stray concurrent processes from earlier launches
    tripled the request rate during the recovery dry-run and produced a
    persistent spray of 429s that read as lake errors until the strays were
    found and killed.

    Deliberately advisory and dependency-free: an ``O_EXCL`` create carrying our
    PID. A lock whose owner is gone is STALE and gets reclaimed (a killed run
    must not wedge the next one), and the lock is always released, including on
    an exception. This does not coordinate with the AG re-ingest -- that remains
    a scheduling constraint -- it only stops this verb racing itself.
    """
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            holder = _lock_holder(path)
            if holder is not None and _pid_alive(holder):
                raise SystemExit(
                    f"another {STAGE} run (pid {holder}) holds {path}. This verb "
                    "must not race itself: the materials endpoint has no "
                    "If-Match, so concurrent read-modify-writes lose updates. "
                    "Wait for it, or pass --lock-file to run against a different "
                    "target.")
            # Stale: the owner died without releasing. Reclaim it.
            try:
                path.unlink()
            except FileNotFoundError:
                pass  # someone else reclaimed it first; retry the create
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield path
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def _lock_holder(path):
    """The PID recorded in a lock file, or None if it is unreadable/garbage."""
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    """True if `pid` is a live process. Signal 0 performs the permission and
    existence checks without delivering anything; EPERM means it exists but is
    owned by someone else, which still counts as alive."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def validate_map(rows, origin):
    """The map must be a non-empty list of row objects carrying every key the
    planner dereferences.

    The read-only producer validates its input (``source_abhiyog``'s
    ``validate_records``); the WRITER had no equivalent, which is backwards. An
    unvalidated map fails late and unevenly: a JSON object iterates as bare keys
    (``'str' object has no attribute 'get'``), and a hand-trimmed row raises
    KeyError at row N -- under ``--apply`` that leaves rows 1..N-1 already
    written with no resume marker.
    """
    if not isinstance(rows, list) or not rows:
        raise SystemExit(
            f"{origin}: expected a non-empty JSON list of map rows, got "
            f"{type(rows).__name__} "
            f"(len={len(rows) if hasattr(rows, '__len__') else '?'})")
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise SystemExit(f"{origin}: row {i} is {type(row).__name__}, not a JSON object")
        missing = [k for k in _REQUIRED_ROW_KEYS if k not in row]
        if missing:
            raise SystemExit(f"{origin}: row {i} (id={row.get('id')!r}) is missing {missing}")
    return rows


def select_targets(rows):
    """Rows eligible for a write: present in the lake AND with a recovered
    number. Everything else is reported, never written.

    ``in_lake`` is tri-state, and "unknown" is NOT "absent": a map produced with
    ``--no-probe`` leaves it blank and a throttled probe leaves it ``error``.
    Collapsing those into ``not_in_lake`` would report a 100% no-op run as
    "none of these materials exist", which is a different (and false) claim.
    """
    targets = []
    skipped = {"not_in_lake": 0, "unrecoverable": 0, "lake_state_unknown": 0}
    for row in rows:
        in_lake = row.get("in_lake")
        if in_lake == "false":
            skipped["not_in_lake"] += 1
        elif in_lake != "true":
            skipped["lake_state_unknown"] += 1
        elif not row.get("case_number"):
            skipped["unrecoverable"] += 1
        else:
            targets.append(row)
    return targets, skipped


def check_guards(doc, record_id):
    """``(outcome_or_None, guards)`` -- identity/type agreement, doc vs map row.

    ``None`` means every guard passed. Otherwise GUARD_MISSING (the doc carries
    no identity fields at all -- see ``_IDENTITY_KEYS``) or GUARD_FAIL (it
    carries them and they disagree). Distinguishing the two matters: a mismatch
    means we were about to stamp a case number onto some OTHER document, while
    an absence means the shape of the stored docs changed underneath us.

    ``additionalType`` is rejected only when it CONTRADICTS charge_sheet, not
    when it is absent -- absence is what the explicit ``material_type`` on the
    PUT exists to cover.
    """
    guards = {
        "recordId": doc.get("jawafdehi:recordId"),
        "officeLevel": doc.get("jawafdehi:officeLevel"),
        "sourceType": doc.get("jawafdehi:sourceType"),
        "additionalType": doc.get("additionalType"),
    }
    if all(doc.get(k) is None for k in _IDENTITY_KEYS):
        return GUARD_MISSING, guards
    additional = guards["additionalType"]
    ok = (str(guards["recordId"]) == str(record_id)
          and guards["officeLevel"] == SPECIAL_OFFICE_LEVEL
          and guards["sourceType"] == EXPECTED_SOURCE_TYPE
          and (additional is None or additional == CHARGE_SHEET_ADDITIONAL_TYPE))
    return (None if ok else GUARD_FAIL), guards


def merge_case_no(doc, row):
    """The CURRENT doc plus the recovered case number. Pure: returns a copy.

    Only ever ADDS keys -- every existing key (notably ``text`` and
    ``associatedMedia``) is carried through untouched, which is what makes the
    wholesale PUT safe.
    """
    merged = copy.deepcopy(doc)
    merged[CASE_NO_KEY] = row["case_number"]
    merged[SOURCE_KEY] = row["source"]
    added = [CASE_NO_KEY, SOURCE_KEY]
    if row.get("ambiguous") == "true":
        # Sources disagreed. Record that, and keep the rejected candidates, so a
        # later binding pass can try both rather than trusting one silently.
        # A LIST, not the map's `;`-joined string: that delimiter exists only
        # because CSV needs a scalar cell. Carrying it into the JSON-LD document
        # would force every consumer to split on `;`, and any consumer reading
        # the field as a single case number would match nothing.
        merged[AMBIGUOUS_KEY] = True
        merged[ALT_KEY] = alt_case_numbers(row)
        added += [AMBIGUOUS_KEY, ALT_KEY]
    return merged, added


def alt_case_numbers(row):
    """The rejected candidates for a row, as a list (the map joins them with
    `;` for its CSV column -- see ``source_abhiyog.build_rows``)."""
    return [a for a in (row.get("alt_case_number") or "").split(";") if a.strip()]


def _backoff_s(interval, attempt):
    """Exponential backoff, capped and clamped to >= 0.

    ``time.sleep()`` raises ``ValueError`` on a negative argument, so a negative
    ``--read-interval`` would abort the walk with a traceback partway through
    rather than degrade. The flag is also rejected at the boundary
    (``cli.nonneg_float``); this covers a direct caller.
    """
    return min(max(interval, 0) * (2 ** attempt), MAX_BACKOFF_S)


def fetch_doc(api, rid, *, retries=4, interval=1.0):
    """GET one material, retrying a throttled/transient read.

    Returns ``(doc, outcome, detail)`` with ``doc=None`` on failure. The
    production materials API rate-limits under bursts: a straight walk of the
    map fires one GET per material back-to-back and a large share come back
    429, which -- treated as a plain error -- would silently demote those
    materials to GET_ERROR and drop them from the backfill.

    Only a genuinely transient failure is retried. A 404 is definitive (the
    material is gone) and ``_FATAL_HTTP`` -- a bad request or bad credentials --
    cannot improve by waiting: retrying an expired token would sleep the full
    ladder on every row, ~15s x 473 rows before reporting the 401 that the first
    response already carried.
    """
    path = material_path(AG_SOURCE, ag_ident(rid))
    last = None
    for attempt in range(max(retries, 0) + 1):
        try:
            return api.get(path), None, ""
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None, GONE, "material not found"
            if exc.code in _FATAL_HTTP:
                return None, GET_FATAL, f"HTTP {exc.code} is not retryable: {exc}"
            last = exc
            if attempt < retries:
                raw = (exc.headers or {}).get("Retry-After")
                wait = (float(raw) if raw and str(raw).isdigit()
                        else _backoff_s(interval, attempt))
                time.sleep(min(max(wait, 0), MAX_BACKOFF_S))
        except Exception as exc:  # noqa: BLE001 - transport errors are retryable
            last = exc
            if attempt < retries:
                time.sleep(_backoff_s(interval, attempt))
    return None, GET_ERROR, f"GET failed after {max(retries, 0) + 1} attempts: {last}"


def plan_one(api, row, *, retries=4, interval=1.0):
    """GET -> guard -> merge for one row. Read-only; performs no write."""
    rid = row["id"]
    plan = {"id": rid, "iri": row["material_iri"],
            "target_case_number": row["case_number"], "source": row["source"],
            "ambiguous": row.get("ambiguous") == "true",
            "alt_case_number": row.get("alt_case_number") or "",
            "outcome": None, "guards": {}, "current_case_number": None,
            "added_keys": [], "detail": "", "merged": None}

    doc, outcome, detail = fetch_doc(api, rid, retries=retries, interval=interval)
    if doc is None:
        plan["outcome"], plan["detail"] = outcome, detail
        return plan

    guard_outcome, guards = check_guards(doc, rid)
    plan["guards"] = guards
    if guard_outcome is not None:
        plan["outcome"] = guard_outcome
        plan["detail"] = (
            f"doc carries none of {list(_IDENTITY_KEYS)} -- the stored shape "
            f"changed; re-verify the ingest path before writing: {guards}"
            if guard_outcome is GUARD_MISSING
            else f"identity/type guard failed: {guards}")
        return plan

    current = doc.get(CASE_NO_KEY)
    plan["current_case_number"] = current
    if current:
        if str(current).strip() == row["case_number"]:
            plan["outcome"] = SKIP_IDEMPOTENT
            plan["detail"] = "already set to the same value"
        else:
            plan["outcome"] = CONFLICT_QUARANTINE
            plan["detail"] = (f"lake has {current!r} != recovered "
                              f"{row['case_number']!r}; never overwritten")
        return plan

    merged, added = merge_case_no(doc, row)
    plan["merged"] = merged
    plan["added_keys"] = added
    plan["outcome"] = WOULD_APPLY
    plan["detail"] = f"add {CASE_NO_KEY}={row['case_number']} [{row['source']}]"
    return plan


def run(args):
    lock = (single_instance(args.lock_file) if getattr(args, "lock_file", "")
            else contextlib.nullcontext())
    with lock:
        return _run(args)


def _run(args):
    logger, run_id, paths = configure_run_logging(STAGE, verbose=args.verbose)
    rows = validate_map(
        json.loads(Path(args.map).read_text(encoding="utf-8")), f"map {args.map}")
    targets, skipped = select_targets(rows)
    if args.limit:
        targets = targets[: args.limit]

    logger.info("map=%d in_lake+recovered=%d skipped(not_in_lake=%d, "
                "unrecoverable=%d, lake_state_unknown=%d) mode=%s", len(rows),
                len(targets), skipped["not_in_lake"], skipped["unrecoverable"],
                skipped["lake_state_unknown"],
                "APPLY" if not args.dry_run else "DRY-RUN")
    if skipped["lake_state_unknown"]:
        # Not the same claim as "absent" -- say so rather than let the summary
        # imply the lake was checked and came back empty.
        logger.warning(
            "%d rows have NO lake verdict (blank => produced with --no-probe; "
            "'error' => the probe was throttled). They are skipped, but that is "
            "'unknown', not 'absent' -- re-run source_abhiyog with probing to "
            "resolve them.", skipped["lake_state_unknown"])

    stats = {"skipped_not_in_lake": skipped["not_in_lake"],
             "skipped_unrecoverable": skipped["unrecoverable"],
             "skipped_lake_state_unknown": skipped["lake_state_unknown"]}
    if not targets:
        # Build no client and demand no credentials for a no-op run.
        logger.info("no writable targets; nothing to do")
        return stats

    api = _build_api(args)
    bodies_path = Path(args.put_bodies) if args.put_bodies else None
    if bodies_path:
        bodies_path.parent.mkdir(parents=True, exist_ok=True)
        bodies_path.write_text("", encoding="utf-8")

    consecutive_failures = 0
    for row in targets:
        plan = plan_one(api, row, retries=args.read_retries,
                        interval=args.read_interval)

        if plan["outcome"] == WOULD_APPLY:
            body = {"material_type": MATERIAL_TYPE, "material": plan["merged"]}
            if bodies_path:
                with bodies_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(
                        {"id": plan["id"], "method": "PUT",
                         "path": f"/materials/{AG_SOURCE}/{ag_ident(plan['id'])}/",
                         "body": body}, ensure_ascii=False) + "\n")
            if not args.dry_run:
                try:
                    api.put_material(AG_SOURCE, ag_ident(plan["id"]),
                                     plan["merged"], material_type=MATERIAL_TYPE)
                    plan["outcome"] = APPLIED
                except Exception as exc:  # noqa: BLE001
                    plan["outcome"] = APPLY_FAILED
                    plan["detail"] = f"PUT failed: {exc}"
                time.sleep(args.write_interval)

        stats[plan["outcome"]] = stats.get(plan["outcome"], 0) + 1
        log_event(logger, paths["events"], run_id=run_id, stage=STAGE,
                  slug=f"ag/{plan['id']}", step="backfill",
                  status=plan["outcome"], detail=plan["detail"])

        # Circuit breaker. A systemic fault -- expired token, missing NGM role,
        # wrong host, API down -- fails identically on every row. Without this
        # the run marches through the whole map issuing doomed requests (one
        # --write-interval pause each) and exits 0 with a full tally of
        # failures, which a wrapper reads as success.
        if plan["outcome"] in _FAILURE_OUTCOMES:
            consecutive_failures += 1
            if (args.max_consecutive_failures
                    and consecutive_failures >= args.max_consecutive_failures):
                stats["ABORTED_CONSECUTIVE_FAILURES"] = consecutive_failures
                logger.error(
                    "ABORT: %d consecutive failures (last: %s -- %s). This looks "
                    "systemic, not per-record; fix it and re-run (the pass is "
                    "idempotent -- already-correct rows come back "
                    "SKIP_IDEMPOTENT).",
                    consecutive_failures, plan["outcome"], plan["detail"])
                break
        else:
            consecutive_failures = 0

    return stats


def _build_api(args):
    """Bearer for production, Basic for a loopback DEV_AUTH server (a local
    server routes any Bearer to OIDC and rejects it, so token-only would fail
    every local run)."""
    if args.api_token:
        return CaseworkApi(base_url=args.api_base_url, token=args.api_token,
                           allow_remote_writes=args.allow_remote_writes)
    return CaseworkApi(base_url=args.api_base_url, basic=basic_auth_from_env(),
                       allow_remote_writes=args.allow_remote_writes)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Backfill jawafdehi:caseNumber onto AG materials.")
    add_common_args(parser)
    parser.add_argument("--map", required=True,
                        help="JSON map from casework.source_abhiyog.")
    parser.add_argument("--put-bodies", default="",
                        help="Optional path; append the exact PUT body that "
                             "would be (or was) sent, one JSON per line.")
    parser.add_argument("--write-interval", type=nonneg_float, default=0.5,
                        help="Seconds to pause after each applied write; the "
                             "materials API rate-limits under bursts.")
    parser.add_argument("--read-retries", type=nonneg_int, default=4,
                        help="Retries for a throttled/transient material GET "
                             "(429s are common on a long run; a 404 is never "
                             "retried).")
    parser.add_argument("--read-interval", type=nonneg_float, default=1.0,
                        help="Base backoff seconds between GET retries "
                             "(exponential, Retry-After honoured).")
    parser.add_argument("--max-consecutive-failures", type=nonneg_int, default=10,
                        help="Abort after this many consecutive failed rows -- "
                             "a streak means a systemic fault (bad token, wrong "
                             "host), not odd records. 0 disables the breaker.")
    parser.add_argument("--lock-file", default=str(DEFAULT_LOCK_FILE),
                        help="Advisory single-instance lock. This verb must not "
                             "race itself -- the materials endpoint has no "
                             "If-Match, so overlapping read-modify-writes lose "
                             "updates. Empty string disables it.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    stats = run(args)
    print_summary(stats, args.dry_run, "backfill ag caseNumber")
    # Nonzero ONLY when the circuit breaker tripped. The sibling verbs always
    # return 0, but a systemic abort is precisely the case a wrapper must not
    # read as success -- per-record outcomes stay in the summary.
    return 1 if stats.get("ABORTED_CONSECUTIVE_FAILURES") else 0


if __name__ == "__main__":
    sys.exit(main())
