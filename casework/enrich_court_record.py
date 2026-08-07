#!/usr/bin/env python
"""Accused binds and case dates, read from the case's own NGM court record.

Zero LLM calls, zero Django, zero source documents. The court record states
these facts rather than inferring them: a defendant is a defendant because a
charge sheet says so, and a verdict date is a verdict date because the Special
Court's docket says so.

WHAT IT WRITES, in one conditional PATCH per case (`CaseworkApi.patch_case`):

  case_start_date  the earliest registration date across the case's court
                   references, and only when the field is currently empty.
  case_end_date    the latest deciding-hearing date, and only when the field is
                   empty AND every court reference on the case has decided.
  entities         the existing bind list with new `accused` binds appended --
                   a whole-list replace of a list merged in application code,
                   never a delta (`merge_entity_binds`).

MEASURED COVERAGE (2026-08-07, anonymous GET, all 307 cases of the FY078/079
census): 307/307 carry a registration date, 306/307 carry an end date, and
307/307 name at least one defendant (1,343 rows). The rule reproduces the
hand-entered convention -- start matches on 46 of 48 published cases, end on
29 of 29 where a deciding hearing exists.

WHY NOT THE SCORED RESOLVER. `casework.entity_resolver` binds the best candidate
above a threshold. NES holds 162,650 person entities dominated by Election
Commission candidate records, so a scored match can name a namesake as the
accused in a corruption case -- the worst error this platform can make. This
module matches on exact name equality within the `person` prefix, and creates
the entity when there is no unique exact match. The failure mode becomes a
duplicate entity, which is a merge, not a defamation.

WHY IT NEVER WRITES `convicted`. `decision_type` sits on the CASE, not on each
defendant. `ठहर` on a 19-defendant case does not say who, and `आंशिक ठहर` means
some were convicted and some cleared. `सफाई` is a whole-case acquittal, so it
alone is distributed to each defendant -- and only ever corrects an unfairly
plain "Accused" label. `charged` is true by construction everywhere else: every
case in this corpus is a Special Court `-CR-` case, so CIAA filed a charge sheet.

Usage:
    uv run python -m casework.enrich_court_record --dry-run --verbose
"""

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field

from casework.common.api import CaseworkApi, EntityAlreadyExists
from casework.common.cli import (
    add_common_args,
    basic_auth_from_env,
    configure_run_logging,
    log_event,
    log_run_footer,
    log_run_header,
    print_summary,
    setup_logging,
)
from casework.common.review import ReviewRow, build_review_file
from casework.common.select import select_for_run
from casework.court_record import court_record_for_case
from casework.entity_identity import entity_slug, prefix_is_creatable
from casework.entity_resolver import normalise_name
from casework.enrich_related_entities import (
    bind_key,
    current_entity_binds,
    merge_entity_binds,
    read_live_prefixes,
    validate_bind_item,
)
from courts.case_status import _order_key, parse_case_status
from jawafdehi_shared.entities.ids import (
    build_entity_iri,
    is_valid_entity_iri,
    parse_entity_iri,
)

logger = logging.getLogger(__name__)

#: Case states this stage may write to. Matches `enrich_related_entities`.
REQUIRED_WRITE_STATE = "DRAFT"

#: `case_status` on a hearing row that decides the case.
DECIDING_STATUS = "फैसला"

#: The whole-case acquittal. The ONLY disposition distributed to each defendant.
ACQUITTAL = "सफाई"

#: NES prefix and schema.org type a court defendant is created under.
PERSON_PREFIX = "person"
PERSON_TYPE = "Person"

#: A verdict is legal only on an accused bind (the `outcome_only_on_accused`
#: CHECK constraint). Sent explicitly so the claim is visible in the request
#: body rather than implied by the API's omitted-outcome fallback.
CHARGED = "charged"
ACQUITTED = "acquitted"


def deciding_hearing(hearings):
    """The hearing that decided the case, or None.

    Picked by MAX `hearing_date_ad` among rows whose `case_status` names a
    verdict -- never by list position. The hearings endpoint does not sort by
    date: on special/079-CR-0151 the 2081-02-22 verdict is returned BEFORE the
    2081-02-21 order that precedes it.
    """
    decided = [h for h in (hearings or ())
               if DECIDING_STATUS in (h.get("case_status") or "")]
    if not decided:
        return None
    return max(decided, key=lambda h: h.get("hearing_date_ad") or "")


def _reference_end(record):
    """`YYYY-MM-DD` this reference decided on, or "" if it has not.

    Two sources, checked in that order: the deciding hearing row, then the
    `case_status` string (`फैसला (मिती: २०८१/०२/२२)`), which
    `courts.case_status.parse_case_status` already converts BS->AD. Across the
    307-case census the two agreed 277 times out of 277, and 29 cases carry only
    the second -- so the fallback is what those 29 depend on, not a tiebreak.
    """
    hearing = deciding_hearing(record.get("hearings"))
    if hearing and hearing.get("hearing_date_ad"):
        return str(hearing["hearing_date_ad"])
    parsed = parse_case_status((record.get("detail") or {}).get("case_status"))
    return parsed.verdict_date_ad.isoformat() if parsed.verdict_date_ad else ""


def start_date(records):
    """The earliest `registration_date_ad` across every court reference, or "".

    Earliest, not first: a case citing two court references started when the
    first of them was registered.
    """
    dates = [str((r.get("detail") or {}).get("registration_date_ad") or "")
             for r in records]
    return min((d for d in dates if d), default="")


def end_date(records):
    """`(value, reason)` -- when the case ended, or "" and why not.

    A case ends when EVERY court reference on it has been decided. One
    undecided reference means the case is still being heard, and
    `case_end_date` is load-bearing on the public site: the frontend's
    `deriveCaseStatus` reads any non-empty value as "concluded" and changes the
    status chip. Half-decided is not decided.
    """
    if not records:
        return "", "no readable court reference"
    ends = [_reference_end(r) for r in records]
    if not any(ends):
        return "", "no decision on record: the case has not been decided"
    if not all(ends):
        undecided = [f"{r['court']}/{r['number']}"
                     for r, e in zip(records, ends) if not e]
        return "", ("not every court reference has decided (still open: "
                    + ", ".join(undecided) + ")")
    return max(ends), ""


@dataclass(frozen=True)
class Resolution:
    """One defendant name's outcome. `how` is the ladder rung it settled on."""
    nes_id: str
    how: str
    reason: str = ""


def _is_person(nes_id):
    """Whether this IRI names a person entity.

    Compares only the IRI's FIRST slash-segment, not the whole prefix and not
    `startswith`: NES nests person categories (`person/politician`), and every
    one of them is still a person, so plain equality against `PERSON_PREFIX`
    would wrongly refuse them. A literal `startswith` check goes too far the
    other way -- it would also match an unrelated `personnel/...` prefix --
    which `.split("/")[0] ==` does not.
    """
    try:
        return parse_entity_iri(nes_id).prefix.split("/")[0] == PERSON_PREFIX
    except Exception:  # noqa: BLE001 - a malformed IRI is simply not a person
        return False


def exact_person_match(api, name):
    """`(nes_id, reason)` -- the ONE person entity whose name is identical, or "".

    Equality after `normalise_name` (NFC, punctuation and case folded), not a
    similarity score. Two entities sharing that exact name is an ambiguity and
    binds nothing: NES holds 13 rows for `संजय प्रसाद यादव`, and picking one by
    score is how a corruption case names the wrong person.

    A SINGLE hit is refused too, when it came from an incomplete search
    window. `CaseworkApi.search_entities` returns a `CandidateList` whose
    `.complete` is False when paging stopped on relevance rather than running
    out of rows -- `संजय प्रसाद यादव` fills a full 50-row page and stops there
    on relevance, and same-title rows do not score identically (that name's
    own duplicates sit at 130.981 and 130.564), so a block of namesakes can
    straddle the page edge. One of them landing inside the fetched window then
    looks "unique" while its twins sit unseen just past it -- the exact
    failure this ladder exists to prevent. The asymmetry is why this fails
    cautious rather than optimistic: a true match sitting outside the window
    just becomes a duplicate entity (a merge), but a truncated window
    promoting a namesake to "unique" binds the wrong person to a corruption
    case (a defamation). `getattr(..., "complete", False)` so a plain list --
    what a stub or a hand-built candidate list returns -- gets the cautious
    answer by default.
    """
    wanted = normalise_name(name)
    if not wanted:
        return "", "empty name"
    results = api.search_entities(name) or ()
    complete = getattr(results, "complete", False)
    hits = {}
    for result in results:
        if not isinstance(result, dict):
            continue
        nes_id = (result.get("id") or "").strip()
        if not is_valid_entity_iri(nes_id) or not _is_person(nes_id):
            continue
        titles = result.get("title") or {}
        if any(normalise_name(t) == wanted for t in (titles.get("ne"), titles.get("en")) if t):
            hits[nes_id] = True
    if not hits:
        return "", "no person entity carries this exact name"
    if len(hits) > 1:
        return "", f"{len(hits)} person entities carry this exact name"
    if not complete:
        return "", ("exactly one exact match, but the search window is "
                    "incomplete: a namesake could be sitting just past the "
                    "edge where this check could not see it")
    return next(iter(hits)), ""


def resolve_defendant(api, name, row_nes_id, *, citation, live_prefixes,
                      run_entities, dry_run):
    """Turn one court-record defendant name into an NES entity id.

    The ladder, top to bottom:
      1. the court row's own `nes_id`  -- a pure copy, no judgment
      2. exactly one person entity with that identical name
      3. create the entity from the court record

    `run_entities` maps a normalised name to an IRI already created THIS RUN and
    is shared across cases on purpose: without it, two cases naming the same
    defendant create two entities. Nothing here raises -- a name that cannot
    become an entity is reported and the case keeps its other defendants. That
    covers the search read too: one transient 502 on one of a case's several
    defendant rows costs that row, not the run.
    """
    row_nes_id = (row_nes_id or "").strip()
    if row_nes_id and is_valid_entity_iri(row_nes_id):
        return Resolution(row_nes_id, "nes_id")

    try:
        matched, why = exact_person_match(api, name)
    except Exception as exc:  # noqa: BLE001 - one bad search costs this name, not the case
        return Resolution("", "failed", f"could not search for a match ({type(exc).__name__})")
    if matched:
        return Resolution(matched, "exact")

    key = normalise_name(name)
    if key in run_entities:
        return Resolution(run_entities[key], "created", "reused from this run")

    if not prefix_is_creatable(PERSON_PREFIX, live_prefixes):
        return Resolution("", "failed", f"{why}; the person prefix is not creatable")
    slug = entity_slug(name)
    if not slug:
        return Resolution("", "failed", f"{why}; the name cannot be slugged")

    iri = build_entity_iri(PERSON_PREFIX, slug)
    if dry_run:
        # POST nothing, but report the IRI an --apply run would use, so the
        # printed patch is the one that would be sent.
        run_entities[key] = iri
        return Resolution(iri, "created", "would create")

    # `slug` is sent explicitly, not left for the server to derive:
    # `normalize_authoring_payload` raises "slug is required" on a payload
    # missing it, since it has no `@id` to fall back on. Omitting it would 422
    # every single creation, which the brief's own stub-backed tests cannot
    # catch because the stub never validates a payload shape.
    payload = {"prefix": PERSON_PREFIX, "slug": slug, "type": PERSON_TYPE, "name": name}
    if citation:
        payload["citation"] = citation
    try:
        created = api.create_entity(payload)
        iri = (created or {}).get("@id") or iri
    except EntityAlreadyExists:
        # The IRI is taken, which means the entity we wanted already exists.
        pass
    except Exception as exc:  # noqa: BLE001 - one failed POST costs this name, not the case
        return Resolution("", "failed", f"could not create the entity ({type(exc).__name__})")
    run_entities[key] = iri
    return Resolution(iri, "created")


@dataclass
class CasePlan:
    """The write for one case, or the reason there isn't one."""
    slug: str
    status: str
    fields: list = field(default_factory=list)
    entities: object = None          # merged full list, or None for "no change"
    if_match: str = ""
    rows: list = field(default_factory=list)
    skips: list = field(default_factory=list)


def _reference_disposition(record):
    """`(decided, is_plain_acquittal)` for one court reference.

    `decided` reuses `_reference_end`'s own truth -- non-empty means decided --
    so this function and `_reference_end` (and therefore `bind_outcome` and
    `end_date`) can never disagree about whether a reference has concluded. A
    reference decided only through the `case_status` paren-date fallback (29
    cases in the census carry only that source, per `_reference_end`'s own
    docstring) carries no outcome text at all, so it is `decided` but never
    `is_plain_acquittal` -- conservative in the direction this function
    already leans, since CHARGED is the default outcome throughout.

    `is_plain_acquittal` is read off the deciding hearing's `decision_type`
    ONLY, and only when that free-text cell says `सफाई` and nothing else
    qualifies it. The hearings API returns raw portal text, and this corpus
    contains compounds that qualify the word rather than standing alone (e.g.
    `आदेश >> आंशिक कसुर ठहर सजाय निर्धारणको लागि पेश गर्ने`). `courts.case_status`'s
    own hearing-decision map puts `आंशिक` first for exactly this reason -- a
    bare substring test on `ठहर` once recorded 593 court_cases as a full
    CONVICTED from a cell that actually said `आंशिक ...ठहर`. The same care
    applies here: a cell naming `आंशिक` or `ठहर` alongside `सफाई` is not a plain
    acquittal, so it is refused rather than guessed at.

    The cell is normalised through `courts.case_status._order_key` before any
    of that testing, not compared raw. The portal spells `आंशिक` two more ways
    in this corpus (`आंशीक`, `आशिंक` -- `_order_key`'s own `_ORDER_SPELLING`
    table says so), and a misspelled qualifier must block ACQUITTED exactly as
    well as the canonical spelling does. `_order_key` is already how this same
    `decision_type`/`order_type` text is normalised elsewhere in that module
    (`outcome_from_hearings`'s own fallback branch), so this reuses the one
    normalisation the corpus's hearing text already goes through, rather than
    hand-copying its variant table and drifting from it later.
    """
    decided = bool(_reference_end(record))
    text = (deciding_hearing(record.get("hearings")) or {}).get("decision_type") or ""
    key = _order_key(text) if text else ""
    plain_acquittal = bool(key) and ACQUITTAL in key and "आंशिक" not in key and "ठहर" not in key
    return decided, plain_acquittal


def bind_outcome(records):
    """The `outcome` every defendant on this case gets.

    ACQUITTED only when EVERY court reference on the case has decided AND every
    one of those decisions was a plain `सफाई` -- a whole-case acquittal, which
    applies to each defendant and can only ever correct an unfairly plain
    "Accused" label. Everything else is CHARGED, which is true by construction:
    CIAA filed a charge sheet on every case in this corpus.

    A single undecided reference must not acquit the rest: half-decided is not
    decided here any more than it is in `end_date`, and stamping ACQUITTED on a
    case that is still being heard is the opposite of true. `_reference_disposition`
    is what keeps the two functions from disagreeing about what "decided" means.

    Never `convicted`. `ठहर` on a 19-defendant case does not say who, and
    `आंशिक ठहर` means some were convicted and some cleared.
    """
    dispositions = [_reference_disposition(r) for r in records]
    if (dispositions
            and all(decided for decided, _ in dispositions)
            and all(acquitted for _, acquitted in dispositions)):
        return ACQUITTED
    return CHARGED


def _accused_binds(api, case, records, *, live_prefixes, run_entities, dry_run):
    """`(items, rows)` -- one bind per named defendant, plus a report row each.

    De-duplicated by name across every court reference on the case, order
    preserved, exactly like `defendant_names` does.
    """
    outcome = bind_outcome(records)
    citation = (records[0].get("detail") or {}).get("material_id", "") if records else ""
    items, rows, seen = [], [], set()
    for record in records:
        for party in record.get("parties") or ():
            if (party.get("side") or "").strip().lower() != "defendant":
                continue
            name = (party.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            got = resolve_defendant(
                api, name, party.get("nes_id"), citation=citation,
                live_prefixes=live_prefixes, run_entities=run_entities,
                dry_run=dry_run)
            row = {"slug": case.get("slug"), "name": name, "how": got.how,
                   "nes_id": got.nes_id, "outcome": outcome, "reason": got.reason,
                   "court_case": f"{record['court']}/{record['number']}"}
            rows.append(row)
            if not got.nes_id:
                continue
            item = {"nes_id": got.nes_id, "relationship_type": "accused",
                    "outcome": outcome,
                    "notes": f"प्रतिवादी — विशेष अदालत मुद्दा {record['number']}"}
            try:
                items.append(validate_bind_item(item))
            except ValueError as exc:
                row.update(how="failed", reason=str(exc))
    return items, rows


def plan_case(api, case, etag, *, live_prefixes, run_entities, dry_run):
    """Build the write for one case. Reads the court record; writes nothing."""
    slug = case.get("slug") or ""
    if (case.get("state") or "").upper() != REQUIRED_WRITE_STATE:
        return CasePlan(slug, "skip-state",
                        skips=[f"state is {case.get('state')!r}, not "
                               f"{REQUIRED_WRITE_STATE}"])

    if "entities" not in case:
        # `case.get("entities") or []` cannot tell "this case has no binds"
        # from "this payload does not carry binds at all" (a trimmed dict from
        # a list endpoint, a projected read). Merging against a false-empty
        # `current` would produce a fully-shaped, validly-formed `entities`
        # list containing only the NEW binds -- which PATCHes clean and
        # silently deletes every bind the case actually has. Refused outright,
        # matching `enrich_related_entities.plan_case_entities`'s own guard for
        # the identical hazard.
        return CasePlan(slug, "no-entities-key",
                        skips=["case payload has no 'entities' key -- absent "
                               "is not empty; refusing to plan a write from "
                               "an incomplete read"])

    records, skips = court_record_for_case(api, case)
    if not records:
        return CasePlan(slug, "no-court-reference", skips=skips)

    fields = []
    if not case.get("case_start_date"):
        if start := start_date(records):
            fields.append(("case_start_date", start))
    if not case.get("case_end_date"):
        end, why = end_date(records)
        if end:
            fields.append(("case_end_date", end))
        elif why:
            skips.append(f"case_end_date left empty: {why}")

    items, rows = _accused_binds(
        api, case, records, live_prefixes=live_prefixes,
        run_entities=run_entities, dry_run=dry_run)

    # `current_entity_binds`, NOT the raw `case["entities"]` list: the read
    # shape keys the relationship type under `type`, and `relationship_type`
    # never appears on a read at all. Merging against the raw list means
    # `bind_key` reads every existing bind as `(nes_id, "")`, so an
    # already-present accused bind never matches the proposed one --
    # `merge_entity_binds` then appends a SECOND bind for the same person, the
    # merged rows carry no `relationship_type` at all (a 400 from
    # `EntityPatchItemSerializer` on every case that already has any bind), and
    # the existing `outcome` gets re-sent instead of staying dropped. The
    # translator produces the PATCH shape and deliberately drops `outcome`, so
    # an existing verdict is preserved rather than reset.
    current = current_entity_binds(case)
    merged = merge_entity_binds(current, items)
    # `merge_entity_binds` appends only what is missing, so an unchanged length
    # means every proposed bind was already present -- send no list at all
    # rather than a destructive replace with identical contents.
    have = {bind_key(b) for b in current}
    entities = merged if any(bind_key(i) not in have for i in items) else None

    status = "would-patch" if (fields or entities is not None) else "nothing-to-do"
    return CasePlan(slug, status, fields=fields, entities=entities,
                    if_match=etag or "", rows=rows, skips=skips)


STAGE = "court_record"


def build_api(args):
    """`CaseworkApi` from parsed args -- Bearer when a token is set, else Basic."""
    if args.api_token:
        return CaseworkApi(args.api_base_url, token=args.api_token,
                           allow_remote_writes=args.allow_remote_writes)
    return CaseworkApi(args.api_base_url, basic=basic_auth_from_env(),
                       allow_remote_writes=args.allow_remote_writes)


def apply_plan(api, plan):
    """Execute a would-patch plan as ONE conditional request.

    Fails closed with no ETag: without If-Match the whole-list replace is
    unconditional and a concurrent edit would be silently clobbered.

    NEITHER RETRIES NOR FORCES. A 412 means the case changed between the read
    and this write, so the merged list is stale and writing it would drop
    someone else's edit. It propagates; `main` records the case as an error and
    emits no bind row, so nothing claims a bind that never landed.
    """
    if not plan.if_match:
        raise ValueError(
            f"refusing to write {plan.slug} with no ETag: the whole-list "
            "replace would be unconditional")
    lists = [] if plan.entities is None else [("entities", plan.entities)]
    return api.patch_case(plan.slug, fields=plan.fields, lists=lists,
                          if_match=plan.if_match)


#: `plan_case` statuses that end a case before any court-record work happens:
#: no `court_read`, `dates`, `defendant_resolve`, `bind_plan` or `patch` event
#: follows one of these, only the `select` event below carrying the mapped
#: status. Anything else falls through to `"selected"`.
#:
#: `"no-entities-key"` reached `plan_case` after this CLI's event vocabulary
#: was first drafted: a case payload with no `entities` key at all cannot be
#: told apart from one that genuinely carries zero binds, so `plan_case`
#: refuses to plan a write rather than merge against a false-empty current
#: list and PATCH a replace that would delete every bind the case actually
#: has (see `plan_case`'s own guard). That refusal is a SKIP exactly like
#: `skip-state` and `no-court-reference` -- nothing downstream was read or
#: planned -- so it is counted and logged the same way, under its own
#: `skip_no_entities_key` status so the events file still records which of
#: the three reasons applied.
_SKIP_SELECT_STATUS = {
    "skip-state": "skip_state",
    "no-court-reference": "skip_no_court_ref",
    "no-entities-key": "skip_no_entities_key",
}


def _log_plan(logger, events, run_id, plan, case):
    """Emit the per-step events for one planned case.

    `run_id`/`stage`/`slug` are passed as explicit keywords on every call
    rather than once via a `**common` dict: `ty` cannot verify that a plain
    `dict[str, str]` splatted into `log_event`'s keyword-only signature never
    lands in `elapsed_ms: int | None` or `level: int`, and flags every call
    site as a type error even though no such collision is possible here.
    `enrich_related_entities.py`'s own `log_event` calls use the same
    explicit-keyword style for the identical reason.
    """
    for row in plan.rows:
        status = {"nes_id": "nes_id_copied", "exact": "exact_match",
                  "created": "created", "failed": "failed"}[row["how"]]
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
                  step="defendant_resolve", status=status,
                  detail=f"{row['name']} -> {row['nes_id'] or row['reason']}")
    if plan.fields:
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
                  step="dates", status="proposed",
                  detail=", ".join(f"{k}={v}" for k, v in plan.fields))
    for skip in plan.skips:
        status = "skip_open_case" if "not every court reference" in skip else "no_source"
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
                  step="dates", status=status, detail=skip)
    log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
              step="bind_plan",
              status="merged" if plan.entities is not None else "no_additions",
              detail=f"{len(plan.rows)} defendant(s) on the court record")


def main(argv=None):
    parser = add_common_args(argparse.ArgumentParser(
        description="Bind court-record defendants and fill the case date fields."))
    args = parser.parse_args(argv)
    setup_logging(args.verbose)
    logger, run_id, paths = configure_run_logging(STAGE, verbose=args.verbose)
    events = paths["events"]
    started = time.time()

    api = build_api(args)
    cases = select_for_run(list(api.iter_cases()), args)
    log_run_header(logger, stage=STAGE, base_url=args.api_base_url,
                   dry_run=args.dry_run, provider=args.provider, model=args.model,
                   n_selected=len(cases), run_id=run_id, paths=paths)

    review = build_review_file(args, stage=STAGE, field_name="accused + case dates",
                               run_id=run_id)
    live_prefixes = read_live_prefixes(api)
    run_entities, stats = {}, {}

    for case in cases:
        slug = case.get("slug") or ""
        try:
            detail, etag = api.get_case_with_etag(slug)
        except Exception as exc:  # noqa: BLE001 - one case's read failure is not the run's
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                      step="court_read", status="unreadable",
                      detail=f"{type(exc).__name__}")
            stats["error"] = stats.get("error", 0) + 1
            continue

        plan = plan_case(api, detail, etag, live_prefixes=live_prefixes,
                         run_entities=run_entities, dry_run=args.dry_run)
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                  step="select", status=_SKIP_SELECT_STATUS.get(plan.status, "selected"))
        if plan.status in _SKIP_SELECT_STATUS:
            stats[plan.status] = stats.get(plan.status, 0) + 1
            continue
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                  step="court_read", status="ok")
        _log_plan(logger, events, run_id, plan, detail)

        generated = "; ".join(
            [f"{k}={v}" for k, v in plan.fields]
            + [f"accused+{len(plan.rows)}" if plan.entities is not None else ""]).strip("; ")
        review.add(ReviewRow(
            slug=slug, status=plan.status,
            before=(f"case_start_date={detail.get('case_start_date')}, "
                    f"case_end_date={detail.get('case_end_date')}, "
                    f"{len(detail.get('entities') or [])} bind(s)"),
            generated=generated,
            note="; ".join(plan.skips)))

        if plan.status == "nothing-to-do":
            stats["nothing-to-do"] = stats.get("nothing-to-do", 0) + 1
            continue
        if args.dry_run:
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                      step="patch", status="dry_run", detail=generated)
            stats["would-patch"] = stats.get("would-patch", 0) + 1
            continue
        try:
            apply_plan(api, plan)
        except Exception as exc:  # noqa: BLE001 - a 412 or a 400 costs this case only
            status = "etag_conflict" if "412" in str(exc) else "rejected"
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                      step="patch", status=status,
                      detail=f"{type(exc).__name__}: {exc}")
            stats["error"] = stats.get("error", 0) + 1
            continue
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                  step="patch", status="applied", detail=generated)
        stats["patched"] = stats.get("patched", 0) + 1

    review.write()
    log_run_footer(logger, stage=STAGE, stats=stats, duration_s=time.time() - started)
    print_summary(stats, args.dry_run, "court-record binder")
    print(f"review file: {review.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
