"""Recover the {material/ag/<id> -> case number} map for AG अभियोगपत्र (the
sourcing-side candidate producer; READ-ONLY, never writes).

The bulk AG scrape landed ~99.7k charge sheets in the lake but lost the case
number, so the indictments cannot be joined to their cases. This CLI rebuilds
that map for the विशेष सरकारी वकील कार्यालय (Special Government Attorney
Office) cohort:

    GET ag.gov.np search API (office_type=1, office_id=2)   -> the full cohort
      -> EXTRACT case number   (court_case_no > file > description, canonicalised)
      -> CROSS-VALIDATE        (>=2 sources disagreeing -> flagged, both kept)
      -> PROBE the lake        (materials.probe_material: in-lake / absent)
      -> EMIT csv + json map   (consumed by casework.backfill_ag_caseno)

Why the ident join is direct: the material IRI ident IS the AG portal record id
(see :func:`casework.common.materials.ag_ident`), so a portal record and a lake
material share a key with no content hashing.

Why three extraction sources: the portal's own ``court_case_no`` column is null
for most rows (it was never backfilled upstream). The case number survives in
the FILENAME (``081-CR-0094_<ts>.pdf``) and, in Devanagari, inside the
``description`` (``(०८१-CR-००९४)``). Rows where none of the three carries a
number are genuinely un-recoverable from metadata: they are banking-offence /
foreign-employment prosecutions that never get a ``-CR-`` number, and the PDF
body does not carry one either (the header reads ``२०८२ सालको मु.द.नं.......``).

This tool NEVER writes -- it has no --apply. It only produces the map that
``casework.backfill_ag_caseno`` later consumes.

Usage:
    uv run python -m casework.source_abhiyog --out work/ag-caseno-map
    uv run python -m casework.source_abhiyog --snapshot cached.json --no-probe
"""
import argparse
import csv
import json
import re
import sys
import urllib.request
from pathlib import Path

from casework.common.api import BROWSER_UA, CaseworkApi
from casework.common.cli import (
    add_common_args, basic_auth_from_env, configure_run_logging, log_event,
    print_summary,
)
from casework.common.materials import ag_ident, material_iri, probe_material

STAGE = "source_abhiyog"
AG_SOURCE = "ag"

#: Special Government Attorney Office. ``/offices/level/1`` on the portal lists
#: exactly ONE office (id=2, alias `sgao`) -- levels 2/3 are the उच्च (High) and
#: जिल्ला (District) offices, whose names do not contain विशेष. Omitting
#: year_id/month_id returns the whole cohort in a single unpaginated response.
AG_SEARCH_URL = (
    "https://ag.gov.np/search-abhiyogpatra"
    "?office_type=1&office_id=2&district_office_id=&year_id=&month_id="
)

#: Devanagari digit -> ASCII. Without this a case number like ०८१-CR-००९४ keeps
#: its Devanagari digits and never matches an ASCII case number.
_DEVA_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
#: Case number inside a filename, before the `_<unix-ts>.<ext>` upload suffix.
_FILE_RE = re.compile(r"^\s*([0-9]{3}-[A-Za-z]+-[0-9]+)_[0-9]+\.[A-Za-z0-9]+\s*$")
#: Case number embedded in a Nepali description, e.g. `(०८१-CR-००९४)`.
_DESC_RE = re.compile(r"([०-९]{3}-[A-Za-z]+-[०-९]+)")
#: A bare court_case_no cell, tolerant of either digit script.
_CCN_RE = re.compile(r"([0-9०-९]{3}-[A-Za-z]+-[0-9०-९]+)")
#: Canonical shape after normalisation.
_CANON_RE = re.compile(r"^([0-9]{3})-([A-Za-z]+)-([0-9]+)$")

#: Highest-trust first. `court_case_no` is the portal's own structured column;
#: the filename is machine-stapled at upload; the description is hand-typed and
#: therefore the most typo-prone, so it loses every tie.
SOURCE_PRIORITY = ("court_case_no", "file", "description")


def canonical_case_no(raw):
    """Normalise a raw case number to `NNN-TYPE-NNNN`, or None if it isn't one.

    Devanagari digits are transliterated and the type token upper-cased, so the
    same case reached via three different fields collapses to one string that
    can be compared for agreement. Leading zeros are preserved -- they are
    significant in a case number.
    """
    if not raw:
        return None
    m = _CANON_RE.match(str(raw).strip().translate(_DEVA_DIGITS))
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2).upper()}-{m.group(3)}"


def extract_case_no(record):
    """All case-number candidates for one portal record, keyed by source.

    Returns ``(case_number, source, candidates, agree)``. ``agree`` is None when
    fewer than two sources produced a value, else True/False. A disagreement is
    NOT resolved here beyond the priority order -- both values are reported so
    the caller can flag the row rather than silently pick one.
    """
    candidates = {}

    ccn = record.get("court_case_no")
    if ccn:
        m = _CCN_RE.search(str(ccn))
        if m and (c := canonical_case_no(m.group(1))):
            candidates["court_case_no"] = c

    fm = _FILE_RE.match(str(record.get("file") or ""))
    if fm and (c := canonical_case_no(fm.group(1))):
        candidates["file"] = c

    dm = _DESC_RE.search(str(record.get("description") or ""))
    if dm and (c := canonical_case_no(dm.group(1))):
        candidates["description"] = c

    case_no = source = None
    for key in SOURCE_PRIORITY:
        if key in candidates:
            case_no, source = candidates[key], key
            break

    agree = None if len(candidates) < 2 else len(set(candidates.values())) == 1
    return case_no, source, candidates, agree


def fiscal_year(record):
    return str(((record.get("month") or {}).get("year") or {}).get("name") or "")


def validate_records(data, origin):
    """The cohort must be a non-empty list of record objects.

    Applied to a CACHED snapshot as well as a freshly fetched one: a truncated
    or hand-edited cache is otherwise trusted blindly, and a JSON object slips
    through to iterate as bare KEYS (``'str' object has no attribute 'get'``)
    or silently yields an empty map.
    """
    if not isinstance(data, list) or not data:
        raise SystemExit(
            f"{origin}: expected a non-empty JSON list of AG records, got "
            f"{type(data).__name__} (len={len(data) if hasattr(data, '__len__') else '?'})")
    if not all(isinstance(r, dict) for r in data):
        raise SystemExit(f"{origin}: every AG record must be a JSON object")
    return data


def fetch_snapshot(url=AG_SEARCH_URL, timeout=120):
    """GET the Special-office cohort from ag.gov.np. External source, not the
    control plane, so it does NOT go through CaseworkApi -- but it reuses the
    same browser UA (the portal serves JSON only to browser-like agents)."""
    req = urllib.request.Request(
        url, headers={"User-Agent": BROWSER_UA, "Accept": "application/json",
                      "X-Requested-With": "XMLHttpRequest"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    return validate_records(data, "ag.gov.np search response")


_VERDICT_LABEL = {True: "true", False: "false", None: "error"}


def build_rows(records, api=None, *, probe=True, interval=1.0, retries=4,
               logger=None, events_path=None, run_id="", probe_cache=None):
    """One output row per portal record (extraction + optional lake probe).

    ``probe_cache`` (id -> verdict label) is read AND written, so a re-run over
    the same cohort re-probes only what it has no answer for -- the probe is a
    ~1s-apart sequential walk of the whole cohort, so an uncached re-run costs
    minutes and needlessly re-pressures the rate limiter.
    """
    rows = []
    cache = probe_cache if probe_cache is not None else {}
    for record in records:
        rid = record.get("id")
        if rid is None or not str(rid).strip():
            # No id means no ident, and ag_ident(None) would cheerfully build
            # `/material/ag/None`. Drop the record rather than emit a row that
            # looks bindable.
            if logger:
                logger.warning("skipping AG record with no id: %r",
                               {k: record.get(k) for k in ("file", "name")})
            continue
        case_no, source, candidates, agree = extract_case_no(record)
        ambiguous = agree is False
        alts = sorted(v for v in set(candidates.values()) if v != case_no)
        in_lake = ""
        if probe and api is not None:
            key = str(rid)
            if key not in cache or cache[key] == "error":
                # Opt in to backoff: this walks the whole cohort against
                # production, which is exactly the burst that gets throttled.
                verdict = probe_material(api, AG_SOURCE, ag_ident(rid),
                                         retries=retries,
                                         interval=interval).verdict
                cache[key] = _VERDICT_LABEL[verdict]
            in_lake = cache[key]
        rows.append({
            "id": rid,
            "material_iri": material_iri(AG_SOURCE, ag_ident(rid)),
            "case_number": case_no or "",
            "source": source or "",
            "ambiguous": "true" if ambiguous else "",
            "alt_case_number": ";".join(alts),
            "in_lake": in_lake,
            "name": (record.get("name") or "").strip(),
            "fy": fiscal_year(record),
            "filing_date_bs": record.get("created_date_np") or "",
            "file": record.get("file") or "",
        })
        if logger and events_path:
            log_event(
                logger, events_path, run_id=run_id, stage=STAGE,
                slug=f"ag/{rid}", step="extract",
                status="recovered" if case_no else "unrecoverable",
                detail=f"case_no={case_no or '-'} source={source or '-'} "
                       f"in_lake={in_lake or '-'}"
                       + (" AMBIGUOUS" if ambiguous else ""))
    return rows


COLUMNS = ("id", "material_iri", "case_number", "source", "ambiguous",
           "alt_case_number", "in_lake", "name", "fy", "filing_date_bs", "file")


def write_outputs(rows, out_prefix):
    """Write `<prefix>.csv` and `<prefix>.json`; returns the two paths.

    The extension is APPENDED, not substituted: `Path.with_suffix` would turn
    `--out map.v2` into `map.csv`, silently collapsing `map.v1` and `map.v2`
    onto the same pair of files.
    """
    prefix = Path(out_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    csv_path = prefix.with_name(prefix.name + ".csv")
    json_path = prefix.with_name(prefix.name + ".json")
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(COLUMNS), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    json_path.write_text(
        json.dumps([{k: r[k] for k in COLUMNS} for r in rows],
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return csv_path, json_path


def summarise(rows):
    recovered = [r for r in rows if r["case_number"]]
    return {
        "records": len(rows),
        "recovered": len(recovered),
        "unrecoverable": len(rows) - len(recovered),
        "ambiguous": sum(1 for r in rows if r["ambiguous"]),
        "in_lake": sum(1 for r in rows if r["in_lake"] == "true"),
        "not_in_lake": sum(1 for r in rows if r["in_lake"] == "false"),
        "probe_error": sum(1 for r in rows if r["in_lake"] == "error"),
    }


def _build_api(args):
    if args.api_token:
        return CaseworkApi(base_url=args.api_base_url, token=args.api_token)
    return CaseworkApi(base_url=args.api_base_url, basic=basic_auth_from_env())


def run(args):
    logger, run_id, paths = configure_run_logging(STAGE, verbose=args.verbose)

    if args.snapshot and Path(args.snapshot).exists():
        records = validate_records(
            json.loads(Path(args.snapshot).read_text(encoding="utf-8")),
            f"cached snapshot {args.snapshot}")
        logger.info("snapshot: %d records from %s", len(records), args.snapshot)
    else:
        records = fetch_snapshot()
        logger.info("fetched %d records from ag.gov.np", len(records))
        if args.snapshot:
            Path(args.snapshot).write_text(
                json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.limit:
        records = records[: args.limit]

    cache_path = Path(args.probe_cache) if args.probe_cache else None
    probe_cache = {}
    if cache_path and cache_path.exists() and not args.refresh_probes:
        probe_cache = json.loads(cache_path.read_text(encoding="utf-8"))
        logger.info("probe cache: %d cached verdicts from %s",
                    len(probe_cache), cache_path)

    api = None if args.no_probe else _build_api(args)
    try:
        rows = build_rows(records, api, probe=not args.no_probe,
                          interval=args.probe_interval,
                          retries=args.probe_retries, logger=logger,
                          events_path=paths["events"], run_id=run_id,
                          probe_cache=probe_cache)
    finally:
        # Persist whatever was learned even if the walk died part-way, so an
        # interrupted probe run does not have to start from zero.
        if cache_path and probe_cache:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(probe_cache, indent=1),
                                  encoding="utf-8")

    csv_path, json_path = write_outputs(rows, args.out)
    stats = summarise(rows)
    logger.info("wrote %s and %s", csv_path, json_path)
    return stats, rows


def build_parser():
    parser = argparse.ArgumentParser(
        description="Recover the ag/<id> -> case number map (read-only).")
    add_common_args(parser)
    parser.add_argument("--out", default="work/ag-caseno-map",
                        help="Output path prefix; writes <prefix>.csv/.json.")
    parser.add_argument("--snapshot", default="",
                        help="Cache path for the raw AG response; reused if it "
                             "exists (re-runs then need no network).")
    parser.add_argument("--no-probe", action="store_true",
                        help="Skip the lake existence probe (offline; leaves "
                             "in_lake blank).")
    parser.add_argument("--probe-interval", type=float, default=1.0,
                        help="Base backoff seconds for a throttled/uncertain "
                             "lake probe (exponential, Retry-After honoured).")
    parser.add_argument("--probe-retries", type=int, default=4,
                        help="Retries for a throttled/uncertain lake probe; "
                             "0 for a single shot.")
    parser.add_argument("--probe-cache", default="",
                        help="JSON path caching id -> in-lake verdict, so a "
                             "re-run only probes what it lacks an answer for.")
    parser.add_argument("--refresh-probes", action="store_true",
                        help="Ignore an existing --probe-cache and re-probe.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not args.dry_run:
        # add_common_args gives every verb --apply; this one has no write path,
        # so say so rather than print a misleading "DRY RUN" and move on.
        print("note: --apply has no effect on source_abhiyog; it is read-only "
              "and only produces the map that casework.backfill_ag_caseno "
              "consumes.", file=sys.stderr)
    stats, _ = run(args)
    # Always read-only: dry_run=True regardless of --apply.
    print_summary(stats, True, "source abhiyog")
    return 0


if __name__ == "__main__":
    sys.exit(main())
