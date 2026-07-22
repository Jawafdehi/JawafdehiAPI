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
concurrent writer's change between our GET and PUT would be lost. Keep this a
single serialized writer and do not run it alongside an AG re-ingest.

Dry-run is the DEFAULT (logs the exact PUT it WOULD send, writes nothing).
``--apply`` opts into writing, and writing to any non-loopback host
additionally requires ``--allow-remote-writes`` (enforced in CaseworkApi).

Usage:
    uv run python -m casework.backfill_ag_caseno --map work/ag-caseno-map.json
    uv run python -m casework.backfill_ag_caseno --map ... --apply --allow-remote-writes
"""
import argparse
import copy
import json
import sys
import time
import urllib.error
from pathlib import Path

from casework.common.api import CaseworkApi
from casework.common.cli import (
    add_common_args, basic_auth_from_env, configure_run_logging, log_event,
    print_summary,
)
from casework.common.materials import ag_ident, material_path

STAGE = "backfill_ag_caseno"
AG_SOURCE = "ag"
MATERIAL_TYPE = "charge_sheet"
EXPECTED_SOURCE_TYPE = "AG_ABHIYOG_PATRA"
SPECIAL_OFFICE_LEVEL = 1

MAX_BACKOFF_S = 30

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
GONE = "GONE"
GET_ERROR = "GET_ERROR"


def select_targets(rows):
    """Rows eligible for a write: present in the lake AND with a recovered
    number. Everything else is reported, never written."""
    targets, skipped = [], {"not_in_lake": 0, "unrecoverable": 0}
    for row in rows:
        if row.get("in_lake") != "true":
            skipped["not_in_lake"] += 1
        elif not row.get("case_number"):
            skipped["unrecoverable"] += 1
        else:
            targets.append(row)
    return targets, skipped


def check_guards(doc, record_id):
    """(ok, guards) -- identity/type/level agreement between doc and map row.

    Guards the blast radius of a wrong id: a mismatch means we are about to
    stamp a case number onto some OTHER document, so it quarantines instead.
    """
    guards = {
        "recordId": str(doc.get("jawafdehi:recordId") or ""),
        "officeLevel": doc.get("jawafdehi:officeLevel"),
        "sourceType": doc.get("jawafdehi:sourceType"),
    }
    ok = (guards["recordId"] == str(record_id)
          and guards["officeLevel"] == SPECIAL_OFFICE_LEVEL
          and guards["sourceType"] == EXPECTED_SOURCE_TYPE)
    return ok, guards


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
        # Sources disagreed. Record that, and keep the rejected candidate, so a
        # later binding pass can try both rather than trusting one silently.
        merged[AMBIGUOUS_KEY] = True
        merged[ALT_KEY] = row.get("alt_case_number") or ""
        added += [AMBIGUOUS_KEY, ALT_KEY]
    return merged, added


def fetch_doc(api, rid, *, retries=4, interval=1.0):
    """GET one material, retrying a throttled/transient read.

    Returns ``(doc, outcome, detail)`` with ``doc=None`` on failure. The
    production materials API rate-limits under bursts: a straight walk of the
    map fires one GET per material back-to-back and a large share come back
    429, which -- treated as a plain error -- would silently demote those
    materials to GET_ERROR and drop them from the backfill. A definitive 404
    is NOT retried: that material is genuinely gone.
    """
    path = material_path(AG_SOURCE, ag_ident(rid))
    last = None
    for attempt in range(retries + 1):
        try:
            return api.get(path), None, ""
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None, GONE, "material not found"
            last = exc
            if attempt < retries:
                raw = (exc.headers or {}).get("Retry-After")
                wait = (float(raw) if raw and str(raw).isdigit()
                        else min(interval * (2 ** attempt), MAX_BACKOFF_S))
                time.sleep(min(wait, MAX_BACKOFF_S))
        except Exception as exc:  # noqa: BLE001 - transport errors are retryable
            last = exc
            if attempt < retries:
                time.sleep(min(interval * (2 ** attempt), MAX_BACKOFF_S))
    return None, GET_ERROR, f"GET failed after {retries + 1} attempts: {last}"


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

    ok, guards = check_guards(doc, rid)
    plan["guards"] = guards
    if not ok:
        plan["outcome"] = GUARD_FAIL
        plan["detail"] = f"identity/type guard failed: {guards}"
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
    logger, run_id, paths = configure_run_logging(STAGE, verbose=args.verbose)
    rows = json.loads(Path(args.map).read_text(encoding="utf-8"))
    targets, skipped = select_targets(rows)
    if args.limit:
        targets = targets[: args.limit]

    logger.info("map=%d in_lake+recovered=%d skipped(not_in_lake=%d, "
                "unrecoverable=%d) mode=%s", len(rows), len(targets),
                skipped["not_in_lake"], skipped["unrecoverable"],
                "APPLY" if not args.dry_run else "DRY-RUN")

    stats = {"skipped_not_in_lake": skipped["not_in_lake"],
             "skipped_unrecoverable": skipped["unrecoverable"]}
    if not targets:
        # Build no client and demand no credentials for a no-op run.
        logger.info("no writable targets; nothing to do")
        return stats

    api = _build_api(args)
    bodies_path = Path(args.put_bodies) if args.put_bodies else None
    if bodies_path:
        bodies_path.parent.mkdir(parents=True, exist_ok=True)
        bodies_path.write_text("", encoding="utf-8")

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
    parser.add_argument("--write-interval", type=float, default=0.5,
                        help="Seconds to pause after each applied write; the "
                             "materials API rate-limits under bursts.")
    parser.add_argument("--read-retries", type=int, default=4,
                        help="Retries for a throttled/transient material GET "
                             "(429s are common on a long run; a 404 is never "
                             "retried).")
    parser.add_argument("--read-interval", type=float, default=1.0,
                        help="Base backoff seconds between GET retries "
                             "(exponential, Retry-After honoured).")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    stats = run(args)
    print_summary(stats, args.dry_run, "backfill ag caseNumber")
    return 0


if __name__ == "__main__":
    sys.exit(main())
