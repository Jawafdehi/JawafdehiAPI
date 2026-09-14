#!/usr/bin/env python
"""Accused binds and the first-instance stage, read from the case's own NGM court record.

No source documents, no LLM and no Django at import -- this stage is pure HTTP.
The court record states these facts rather than inferring them: a defendant is a
defendant because a charge sheet says so, and a verdict date is a verdict date
because the Special Court's docket says so.

WHICH DOCKET. Only a TRIAL docket is read: `court_record.trial_refs` keeps the
Special Court prosecutions and drops everything else. The case number cannot
make that call -- all 1,824 mapped appeals carry a `-CR-` number too, at
`supreme` -- so the court does. Every other docket is reported for the appeal
enricher and never staged, and its parties never reach the binder.

WHAT IT WRITES, in one conditional PATCH per case (`CaseworkApi.patch_case`):

  dates     the case's whole stage document, with one `initial` stage per trial
            docket carrying that docket's own registration and decision date
            (`casework.case_stages.merge_trial_stages`). A whole-document
            replace, so the merge preserves every stage the case already had.
  entities  the existing bind list with new `accused` binds appended -- a
            whole-list replace of a list merged in application code, never a
            delta (`merge_entity_binds`).

The two retired columns (`case_start_date`, `case_end_date`) are no longer
written. They collapsed every docket into one span, which under stages
fabricates a single proceeding across trial and appeal.

WHY IT NEVER MATCHES AN EXISTING ENTITY. NES holds 162,650 person entities
dominated by Election Commission candidate records, so any match -- scored or
exact -- can name a namesake as the accused in a corruption case, the worst
error this platform can make. Every defendant therefore gets a NEW entity,
suffixed past any slug collision. The cost is duplicate people, which is a
merge; the alternative is a defamation.

THE ONE COMPARISON LEFT is same-case: a name already bound as accused on THIS
case is skipped rather than bound again (`already_bound`). Its scope is one
case's own roster, and without it a re-run appends a second entity for every
defendant already bound.

WHY IT NEVER WRITES `convicted`. `decision_type` sits on the CASE, not on each
defendant. `ठहर` on a 19-defendant case does not say who, and `आंशिक ठहर` means
some were convicted and some cleared. `सफाई` is a whole-case acquittal, so it
alone is distributed to each defendant. `charged` is true by construction
everywhere else: every case in this corpus is a Special Court prosecution, so
CIAA filed a charge sheet.

Usage:
    uv run python -m casework.enrich_court_record --dry-run --verbose
"""

import argparse
import logging
import re
import sys
import time
import urllib.error
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
from casework.common.review import ReviewRow, build_review_file, md_cell
from casework.common.select import select_for_run
from casework.case_stages import (
    deciding_hearing,
    docket_label,
    merge_trial_stages,
    reference_end,
    reference_start,
    trial_stage,
)
from casework.court_record import (
    court_record_for_case,
    is_defendant,
    other_refs,
    party_name,
    split_alias,
    trial_refs,
)
from casework.entity_identity import (
    MAX_SLUG_LENGTH,
    entity_slug,
    prefix_is_creatable,
)
from casework.entity_resolver import normalise_name
from casework.enrich_related_entities import (
    ACCUSED_SECTION,
    bind_key,
    bind_relationship_type,
    current_entity_binds,
    merge_entity_binds,
    read_live_prefixes,
    validate_bind_item,
)
from courts.case_status import _order_key
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


#: How many `-N` slug retries before a name is reported instead of bound. A
#: common name genuinely collides a handful of times; an uncapped loop would
#: hammer the API on a pathological one.
MAX_SLUG_SUFFIX = 20

#: Trailing `-2`, `-3` on a created slug. Stripped before comparison so a
#: re-run recognises the entity a previous run minted for the same name.
_SUFFIX = re.compile(r"-\d+$")


def bound_accused_keys(case):
    """Normalised names AND de-suffixed slugs already bound as accused here.

    Two independent keys because either can be missing: `display_name` is None
    when the NES resolver cannot reach the entity, and a hand-authored entity's
    slug need not match what `entity_slug` would produce from the court name.
    """
    keys = set()
    for bind in (case.get("entities") or []):
        if bind_relationship_type(bind) != ACCUSED_SECTION:
            continue
        if name := normalise_name(bind.get("display_name") or ""):
            keys.add(name)
        try:
            slug = parse_entity_iri((bind.get("nes_id") or "").strip()).slug
        except Exception:  # noqa: BLE001 - a malformed IRI contributes no key
            continue
        keys.add(_SUFFIX.sub("", slug))
    return keys


def already_bound(name, keys):
    """Whether this court-record name is already an accused bind on the case.

    The ONLY name comparison left in this module, and it is scoped to one
    case's own roster -- roughly five names, not the 162,650 person entities a
    NES search would weigh. A false match costs one missing bind; without it a
    re-run appends a second entity for every defendant already bound.
    """
    if normalise_name(name) in keys:
        return True
    slug = entity_slug(name)
    return bool(slug) and _SUFFIX.sub("", slug) in keys


def _suffixed(slug, n):
    """`slug-N`, trimmed so the suffix cannot push it past MAX_SLUG_LENGTH."""
    tail = f"-{n}"
    return slug[:MAX_SLUG_LENGTH - len(tail)].rstrip("-") + tail


def resolve_defendant(api, name, row_nes_id, *, citation, live_prefixes, dry_run):
    """One defendant name to an NES entity id. Never reuses an existing entity.

    Rung 1 is the party row's own `nes_id` -- a pointer NGM wrote, not a name
    comparison. Otherwise a new entity is created, suffixing the slug past any
    collision. A 409 used to refuse the bind; it is now the ordinary path.

    Nothing here raises: a name that cannot become an entity is reported and
    the case keeps its other defendants.
    """
    row_nes_id = (row_nes_id or "").strip()
    if row_nes_id and is_valid_entity_iri(row_nes_id):
        # `_is_person` too, not just a well-formed IRI: the create rung can
        # only ever produce a `person`, so without this rung 1 is the one way a
        # non-person IRI reaches an `accused` bind -- an office or a company
        # named as the accused individual.
        if not _is_person(row_nes_id):
            return Resolution("", "failed",
                              f"the court row's nes_id {row_nes_id} is not a "
                              f"{PERSON_PREFIX} entity")
        return Resolution(row_nes_id, "nes_id")

    if live_prefixes is None:
        # NOT a judgement on `person` -- nothing was checked.
        # `read_live_prefixes` returns None for exactly this case, but
        # `prefix_is_creatable` folds None and [] to the same empty set.
        return Resolution("", "failed",
                          "the live entity prefix list could not be read, so "
                          f"{PERSON_PREFIX!r} was never checked -- retry this case")
    if not prefix_is_creatable(PERSON_PREFIX, live_prefixes):
        return Resolution("", "failed", "the person prefix is not creatable")
    base = entity_slug(name)
    if not base:
        return Resolution("", "failed", "the name cannot be slugged")

    if dry_run:
        return Resolution(build_entity_iri(PERSON_PREFIX, base), "created",
                          "would create -- a dry run makes no POST, so the "
                          "applied slug may carry a -N suffix if this one is "
                          "already taken")

    for attempt in range(1, MAX_SLUG_SUFFIX + 1):
        slug = base if attempt == 1 else _suffixed(base, attempt)
        # `slug` is sent explicitly: `normalize_authoring_payload` raises
        # "slug is required" on a payload missing it, since it has no `@id` to
        # fall back on.
        payload = {"prefix": PERSON_PREFIX, "slug": slug,
                   "type": PERSON_TYPE, "name": name}
        if citation:
            payload["citation"] = citation
        try:
            created = api.create_entity(payload)
        except EntityAlreadyExists:
            continue
        except Exception as exc:  # noqa: BLE001 - one failed POST costs this name, not the run
            return Resolution("", "failed",
                              f"could not create the entity ({type(exc).__name__})")
        return Resolution((created or {}).get("@id")
                          or build_entity_iri(PERSON_PREFIX, slug), "created")
    return Resolution("", "failed",
                      f"every slug from {base} to {_suffixed(base, MAX_SLUG_SUFFIX)} "
                      f"is already taken ({MAX_SLUG_SUFFIX} attempts)")


@dataclass
class CasePlan:
    """The write for one case, or the reason there isn't one."""
    slug: str
    status: str
    fields: list = field(default_factory=list)
    stages: object = None            # the whole {"stages": [...]} document, or None
    entities: object = None          # merged full list, or None for "no change"
    if_match: str = ""
    rows: list = field(default_factory=list)
    skips: list = field(default_factory=list)


def _reference_disposition(record):
    """`(decided, is_plain_acquittal)` for one court reference.

    `decided` reuses `case_stages.reference_end`'s own truth -- non-empty means
    decided -- so this function and the stage builder can never disagree about
    whether a reference has concluded. A reference decided only through the
    `case_status` paren-date fallback (29 cases in the census carry only that
    source) carries no outcome text at all, so it is `decided` but never
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
    decided = bool(reference_end(record))
    text = (deciding_hearing(record.get("hearings")) or {}).get("decision_type") or ""
    key = _order_key(text) if text else ""
    plain_acquittal = bool(key) and ACQUITTAL in key and "आंशिक" not in key and "ठहर" not in key
    return decided, plain_acquittal


def bind_outcome(records, unread=0):
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

    `unread` counts the case's references this run did NOT fetch -- everything
    `trial_refs` filters out. An unread reference counts as undecided, because
    that is the only safe reading: a Special Court acquittal with a live appeal
    at `supreme` is not an acquittal, and this value is what
    `enrich_allegations.append_acquittal_line` turns into a public
    "सफाइ दिने ठहर" sentence.

    Never `convicted`. `ठहर` on a 19-defendant case does not say who, and
    `आंशिक ठहर` means some were convicted and some cleared.
    """
    if unread:
        return CHARGED
    dispositions = [_reference_disposition(r) for r in records]
    if (dispositions
            and all(decided for decided, _ in dispositions)
            and all(acquitted for _, acquitted in dispositions)):
        return ACQUITTED
    return CHARGED


def _accused_binds(api, case, records, *, live_prefixes, dry_run, unread=0):
    """`(items, rows)` -- binds, and a report row for each defendant.

    `records` carries TRIAL dockets only; `court_record.trial_refs` does that
    filtering upstream, so the appeal docket's roster never reaches here and
    cannot mint a second entity for a person this case already names. `unread`
    carries the count of what that filter dropped, for `bind_outcome`.

    De-duplicated by normalised name WITHIN the case: one person named on two
    of the case's trial dockets is one bind. Two genuinely distinct defendants
    who share a name collapse here too -- accepted, and the reason the row
    count is reported beside the party count.
    """
    outcome = bind_outcome(records, unread)
    citation = (records[0].get("detail") or {}).get("material_id", "") if records else ""
    bound = bound_accused_keys(case)
    items, rows, seen = [], [], set()
    for record in records:
        for party in record.get("parties") or ():
            if not is_defendant(party):
                continue
            # The alias prefix is stripped before ANY of this: the name that
            # resolves and creates the entity must be the legal one.
            name, aliases = split_alias(party_name(party))
            key = normalise_name(name)
            if not name or key in seen:
                continue
            seen.add(key)
            row = {"slug": case.get("slug"), "name": name, "outcome": outcome,
                   "aliases": aliases,
                   "court_case": f"{record['court']}/{record['number']}"}
            if already_bound(name, bound):
                rows.append({**row, "how": "skipped", "nes_id": "",
                             "reason": "already an accused bind on this case"})
                continue
            got = resolve_defendant(api, name, party.get("nes_id"),
                                    citation=citation,
                                    live_prefixes=live_prefixes, dry_run=dry_run)
            row.update(how=got.how, nes_id=got.nes_id, reason=got.reason)
            rows.append(row)
            if not got.nes_id:
                continue
            # The stripped alias rides on the bind, not the entity name: the
            # court called this person something, and dropping that on the floor
            # loses the only place the record says so.
            note = f"प्रतिवादी — विशेष अदालत मुद्दा {record['number']}"
            if aliases:
                note += f"; अदालतको अभिलेखमा: {' भन्ने '.join([*aliases, name])}"
            item = {"nes_id": got.nes_id, "relationship_type": ACCUSED_SECTION,
                    "outcome": outcome, "notes": note}
            try:
                items.append(validate_bind_item(item))
            except ValueError as exc:
                # Unreachable today -- `resolve_defendant` validates the IRI
                # at rung 1 and builds a canonical one otherwise. Kept
                # self-consistent so it stays harmless if that changes: `main`
                # reads the outcome
                # off the first row that carries one, and `accused_table`
                # prints it -- so leaving it set reports a bind, an outcome and
                # an entity for a defendant no bind was written for.
                row.update(how="failed", nes_id="", reason=str(exc))
    return items, rows


def plan_case(api, case, etag, *, live_prefixes, dry_run, court_record=None):
    """Build the write for one case; writes nothing.

    Reads the case's TRIAL dockets itself unless `court_record` -- a
    `(records, skips)` pair -- is supplied.
    """
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
        # silently deletes every bind the case actually has.
        return CasePlan(slug, "no-entities-key",
                        skips=["case payload has no 'entities' key -- absent "
                               "is not empty; refusing to plan a write from "
                               "an incomplete read"])

    trials = trial_refs(case)
    others = other_refs(case)
    if court_record is not None:
        records, skips = court_record
    else:
        records, skips = court_record_for_case(api, case, refs=trials)
    for court, number, _ in others:
        skips.append(f"court reference {court}/{number} is not a first-instance "
                     "prosecution: no stage written, left for the appeal enricher")
    if not trials:
        skips.append("no trial docket on the case: no stage can be written")
    if not records:
        return CasePlan(slug, "no-court-reference", skips=skips)

    # A DATE makes a stage; a docket alone does not. Migration 0068 took the
    # same decision: a dateless `initial` record reads as an open court
    # proceeding, so seeding one from a bare docket flips a dateless draft's
    # derived status to ONGOING on no evidence at all.
    datedd = [r for r in records if reference_start(r) or reference_end(r)]
    stages = None
    if "dates" not in case:
        # The same absent-is-not-empty hazard the entities key has: `/dates` is
        # a whole-document replace, so merging against a false-empty stage list
        # would delete every stage the case actually carries.
        skips.append("case payload has no 'dates' key -- absent is not empty; "
                     "refusing to plan a stage write from an incomplete read")
    else:
        document = case.get("dates") or {"stages": []}
        stored = document.get("stages") or []
        merged_stages, changes = merge_trial_stages(
            stored, [trial_stage(r) for r in datedd])
        skips.extend(changes)
        if merged_stages != stored:
            stages = {**document, "stages": merged_stages}

    items, rows = _accused_binds(api, case, records, live_prefixes=live_prefixes,
                                 dry_run=dry_run, unread=len(others))

    # `current_entity_binds`, NOT the raw `case["entities"]` list: the read
    # shape keys the relationship type under `type`, and `relationship_type`
    # never appears on a read at all. Merging against the raw list means
    # `bind_key` reads every existing bind as `(nes_id, "")`, so an
    # already-present accused bind never matches the proposed one.
    current = current_entity_binds(case)
    merged = merge_entity_binds(current, items)
    # `merge_entity_binds` appends only what is missing, so an unchanged length
    # means every proposed bind was already present -- send no list at all
    # rather than a destructive replace with identical contents.
    have = {bind_key(b) for b in current}
    entities = merged if any(bind_key(i) not in have for i in items) else None

    fields = [("dates", stages)] if stages is not None else []
    status = "would-patch" if (fields or entities is not None) else "nothing-to-do"
    return CasePlan(slug, status, fields=fields, entities=entities,
                    stages=stages, if_match=etag or "", rows=rows, skips=skips)


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
#: status -- which makes these three TERMINAL, and so the only `select`
#: statuses that may be distinctive. Anything else falls through to `"ok"`.
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


#: Prefix `court_record_for_case` puts on every per-reference read failure it
#: reports (`f"court reference {court}/{number} could not be read (...)"`).
#: `_log_plan` matches on this exact prefix to route those skips to
#: `court_read`/`unreadable` rather than `dates` -- a reference that 404s cost
#: this case its defendants and/or its dates from THAT reference, and it is
#: not a fact about date-derivation the way "case_end_date left empty: ..."
#: is. Checked as a prefix, not a substring: the OTHER skip this function
#: sees, "case_end_date left empty: not every court reference has decided
#: ...", contains the words "court reference" too, just never at position 0.
_COURT_READ_FAILURE_PREFIX = "court reference "


#: Marker `plan_case` puts in every skip naming a docket it refused to stage.
#: Matched as a substring, not a prefix: the line opens with the court
#: reference, exactly like `_COURT_READ_FAILURE_PREFIX`'s lines do.
_NON_TRIAL_SKIP_MARKER = "is not a first-instance prosecution"

#: Words that mark a `merge_trial_stages` change line, so `_log_plan` can label
#: it `stage_change` rather than dropping it in the `no_source` catch-all --
#: "this run would move a date" and "this case has no source" are opposite
#: facts, and the dry run printed the first under the second's name.
_STAGE_CHANGE_MARKERS = {"->", "DROPPED", "kept", "adopted", "added"}

#: Marker on the skip `plan_case` raises when the payload carries no `dates`
#: key at all. The stage write is REFUSED there, not satisfied -- and the case
#: can still land on `nothing-to-do`, whose ledger line would otherwise report
#: the refusal as a confirmed match.
_NO_DATES_KEY_MARKER = "has no 'dates' key"


#: The ladder rung -- or hold decision -- each `plan.rows` entry settled on,
#: spelled for the events file. It rides in the event's DETAIL, not its
#: status: every event this function emits is an INTERMEDIATE step, and
#: `casework.ledger.build_ledger` treats any status outside
#: `NON_OUTCOME_STATUSES` as the case's outcome for the stage. A per-defendant
#: `failed` or a per-case `merged` would therefore be recorded as what this
#: stage DID to the case, which on a dry run is nothing at all -- and `failed`
#: is a real terminal status for `casework.convert`, so it cannot simply be
#: added to that shared frozenset. Every sibling enricher resolves this the
#: same way: intermediate steps report `ok` and put the specifics in `detail`
#: (`step="source", status="ok"`, `step="resolve", status="ok"`), leaving
#: distinctive statuses to the one terminal event per case.
_RUNG_WORDS = {"nes_id": "nes_id_copied", "created": "created",
               "skipped": "skipped_already_bound", "failed": "failed"}

#: Short label per rung for the tally lines, in print order. Insertion order is
#: the display order, so a case's line and the run footer read the same way.
_RUNG_LABELS = {"nes_id_copied": "copied", "created": "created",
                "skipped_already_bound": "already bound", "failed": "failed"}


def rung_summary(rows):
    """`"5 defendant(s): 0 copied, 4 created, 1 already bound, 0 failed"`.

    EVERY rung prints, including its zero. `created` is the load-bearing number
    in this corpus: nothing here matches an existing entity by name, so a high
    created count is the expected shape and tells an operator to expect
    duplicate entities rather than reuse. A tally that dropped its zeroes would
    hide exactly the number worth reading.

    `_RUNG_WORDS[...]` is indexed, not `.get`: an unrecognised `how` is a bug in
    `resolve_defendant`, and counting it under some fallback rung would report a
    tally that silently does not add up.
    """
    counts = dict.fromkeys(_RUNG_LABELS.values(), 0)
    for row in rows:
        counts[_RUNG_LABELS[_RUNG_WORDS[row["how"]]]] += 1
    return (f"{len(rows)} defendant(s): "
            + ", ".join(f"{n} {label}" for label, n in counts.items()))


def court_read_summary(records):
    """`"2 court reference(s), 7 part(ies), 5 defendant(s)"` for one case.

    The `court_read` event carried no detail at all, so a case whose references
    read fine but named nobody looked identical in the log to one that named
    twenty. Counts every party row, not just the bindable ones -- a reference
    refused for its case type still reports what it held.
    """
    parties = sum(len(r.get("parties") or ()) for r in records)
    defendants = sum(1 for r in records for p in (r.get("parties") or ())
                     if is_defendant(p))
    return (f"{len(records)} court reference(s), {parties} part(ies), "
            f"{defendants} defendant(s)")


def accused_table(rows):
    """One Markdown row per court-record defendant, or "" if the case had none.

    `generated` can only ever say `accused+21`, and the review file's summary
    table counts characters -- so without this a reviewer cannot see WHICH people
    a case would bind or WHAT verdict each bind claims. Both are the whole point
    of reviewing this stage.

    `Outcome` is blank for a name that resolved to nothing: no bind is written,
    so quoting the case's outcome against it would claim a verdict was recorded
    for someone who was never bound.

    A stripped alias is printed beside the name it was stripped from, so a
    reviewer can see BOTH what the court wrote and what this run will bind --
    the two differ on 1.3% of defendants and that is exactly where a wrong
    entity would be hardest to spot.
    """
    if not rows:
        return ""
    out = ["| # | Defendant | Outcome | Resolution | NES entity |",
           "|---|---|---|---|---|"]
    for i, row in enumerate(rows, 1):
        bound = row["nes_id"]
        rung = _RUNG_WORDS.get(row["how"], row["how"])
        entity = f"`{md_cell(bound)}`" if bound else md_cell(row["reason"]) or "—"
        who = md_cell(row["name"])
        if row.get("aliases"):
            who += f" _(भन्ने: {md_cell(', '.join(row['aliases']))})_"
        out.append(f"| {i} | {who} | {md_cell(row['outcome']) if bound else '—'} "
                   f"| {rung} | {entity} |")
    return "\n".join(out)


def _rung_counts(rows):
    """`(resolved_count, skipped_count)` over a plan's per-defendant rows.

    Resolved means this run named an entity -- so a `failed` row (an
    unsluggable name, an exhausted suffix, a POST error) counts as neither, and
    neither does a name already bound on the case. Counting either as resolved
    is how "accused+N" once reported a phantom bind.
    """
    skipped = sum(1 for row in rows if row["how"] == "skipped")
    resolved = sum(1 for row in rows if row["how"] not in ("skipped", "failed"))
    return resolved, skipped


def _resolve_detail(row):
    """What one defendant resolved to -- the IRI, its caveat, or why it failed.

    `nes_id or reason` alone discards the reason on every row that has both,
    which is exactly the two rows whose reason carries a warning: a dry run's
    "would create" (`--apply` refuses it if the slug is taken) and a
    "reused from this run". Both then read as a plain settled bind.
    """
    if row["nes_id"] and row["reason"]:
        return f"{row['nes_id']} ({row['reason']})"
    return row["nes_id"] or row["reason"]


def _log_plan(logger, events, run_id, plan):
    """Emit the per-step events for one planned case. Returns `(skipped, resolved)`.

    Every event here is intermediate and therefore `ok`-statused; see
    `_RUNG_WORDS` for why, and `main` for the terminal events that follow.
    Both counts are returned so `main` can build its own "accused+N" and
    already-bound text from the SAME numbers this summary line reports,
    rather than recomputing them from `plan.rows` a second time.

    `run_id`/`stage`/`slug` are passed as explicit keywords on every call
    rather than once via a `**common` dict: `ty` cannot verify that a plain
    `dict[str, str]` splatted into `log_event`'s keyword-only signature never
    lands in `elapsed_ms: int | None` or `level: int`, and flags every call
    site as a type error even though no such collision is possible here.
    `enrich_related_entities.py`'s own `log_event` calls use the same
    explicit-keyword style for the identical reason.
    """
    resolved_count, skipped_count = _rung_counts(plan.rows)
    for row in plan.rows:
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
                  step="defendant_resolve", status="ok",
                  detail=f"{_RUNG_WORDS[row['how']]}: {row['name']} -> "
                         f"{_resolve_detail(row)}")
    if plan.stages is not None:
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
                  step="dates", status="ok",
                  detail="proposed " + stage_summary(plan.stages))
    code_skips = 0
    for skip in plan.skips:
        if _NON_TRIAL_SKIP_MARKER in skip:
            # CHECKED FIRST. This line opens with "court reference ", the same
            # prefix `_COURT_READ_FAILURE_PREFIX` matches, so the read-failure
            # branch below would otherwise claim every non-trial docket and
            # report a healthy reference as unreadable.
            code_skips += 1
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
                      step="bind_plan", status="ok", detail=skip)
            continue
        if skip.startswith(_COURT_READ_FAILURE_PREFIX):
            # A per-reference read failure, not a date fact: `plan.status` is
            # not one of the SKIP statuses here (this case had at least one
            # readable reference, or `plan_case` would have returned
            # "no-court-reference"), so the earlier `court_read`/`ok` event
            # stands -- this event says the SAME court read was only partial.
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
                      step="court_read", status="ok", detail=f"unreadable: {skip}")
            continue
        # A stage change line is not a missing source -- it says what this run
        # would alter and is the line a reviewer reads before an --apply.
        kind = "stage_change" if _STAGE_CHANGE_MARKERS & set(skip.split()) else "no_source"
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
                  step="dates", status="ok", detail=f"{kind}: {skip}")
    # "resolved", not "on the court record": a skipped row and a failed row
    # both sit in `plan.rows` but neither was resolved (see `_rung_counts`).
    summary = (f"{'merged' if plan.entities is not None else 'no_additions'}: "
               f"{resolved_count} defendant(s) resolved"
               f" [{rung_summary(plan.rows)}]")
    if code_skips:
        summary += f"; {code_skips} court reference(s) left for the appeal enricher"
    if skipped_count:
        summary += f"; {skipped_count} name(s) already bound on this case"
    log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
              step="bind_plan", status="ok", detail=summary)
    return skipped_count, resolved_count


def stage_summary(document):
    """`"initial 2022-08-01..2024-06-04 (special/079-CR-0151)"` per stage."""
    parts = []
    for stage in (document or {}).get("stages") or ():
        span = f"{stage.get('start') or '?'}..{stage.get('end') or 'open'}"
        parts.append(f"{stage.get('stage')} {span} "
                     f"({docket_label(stage.get('courtcase_iri')) or 'no docket'})")
    return "; ".join(parts) or "no stages"


def stage_table(plan):
    """One Markdown row per stage this case would carry, or "" if none change."""
    stages = (plan.stages or {}).get("stages") or []
    if not stages:
        return ""
    lines = ["| Stage | Start | End | Docket |", "|---|---|---|---|"]
    for stage in stages:
        lines.append(f"| {md_cell(stage.get('stage'))} "
                     f"| {md_cell(stage.get('start') or '—')} "
                     f"| {md_cell(stage.get('end') or 'open')} "
                     f"| {md_cell(docket_label(stage.get('courtcase_iri')) or '—')} |")
    return "\n".join(lines)


def main(argv=None):
    parser = add_common_args(argparse.ArgumentParser(
        description="Bind court-record defendants and write the case's "
                    "first-instance stage."))
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

    review = build_review_file(args, stage=STAGE,
                               field_name="accused + first-instance stage",
                               run_id=run_id)
    live_prefixes = read_live_prefixes(api)
    stats = {}
    # Every per-defendant row the run produces, so the footer can tally the
    # rungs across cases. `stats` cannot carry this: it counts CASES by status.
    all_rows = []

    # ONE pass. The second pass existed only to plan every case against a
    # cross-case defendant-name index; with the hold gone there is nothing to
    # build before planning, so each case is read once and planned immediately.
    for i, case in enumerate(cases, 1):
        slug = case.get("slug") or ""
        if not slug:
            log_event(logger, events, run_id=run_id, stage=STAGE, slug="",
                      step="court_read", status="unreadable",
                      detail=f"case {i} of {len(cases)} has no slug -- cannot "
                             "be read")
            stats["error"] = stats.get("error", 0) + 1
            continue
        try:
            case_detail, etag = api.get_case_with_etag(slug)
        except Exception as exc:  # noqa: BLE001 - one case's read failure is not the run's
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                      step="court_read", status="unreadable",
                      detail=f"{type(exc).__name__}")
            stats["error"] = stats.get("error", 0) + 1
            continue
        logger.info("read %d/%d selected cases (%s)", i, len(cases), slug)

        records, read_skips = court_record_for_case(
            api, case_detail, refs=trial_refs(case_detail))
        plan = plan_case(api, case_detail, etag, live_prefixes=live_prefixes,
                         dry_run=args.dry_run,
                         court_record=(records, read_skips))
        # `detail=` carries `plan.skips` even on a clean "selected": a case can
        # reach "would-patch"/"nothing-to-do" with a partially-unreadable court
        # record, and the SAME line tells an operator replaying the ledger
        # WHICH state or WHICH missing reference a bare status cannot.
        # `"ok"` -- not `"selected"` -- for a case that proceeds: selection is
        # an intermediate step, and any status outside
        # `casework.ledger.NON_OUTCOME_STATUSES` is recorded as the case's
        # outcome for this stage.
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                  step="select", status=_SKIP_SELECT_STATUS.get(plan.status, "ok"),
                  detail="; ".join(plan.skips))
        if plan.status in _SKIP_SELECT_STATUS:
            stats[plan.status] = stats.get(plan.status, 0) + 1
            continue
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                  step="court_read", status="ok",
                  detail=court_read_summary(records))
        skipped_count, resolved_count = _log_plan(logger, events, run_id, plan)
        all_rows.extend(plan.rows)

        generated_parts = []
        if plan.stages is not None:
            generated_parts.append(stage_summary(plan.stages))
        if plan.entities is not None:
            # The outcome rides on every accused bind this case writes, so a
            # reviewer reading only the summary line still sees which verdict
            # is being claimed. `bind_outcome` is per case, not per defendant.
            outcome = next((r["outcome"] for r in plan.rows if r["nes_id"]), "")
            generated_parts.append(f"accused+{resolved_count}"
                                   + (f" ({outcome})" if outcome else ""))
        if skipped_count:
            generated_parts.append(f"{skipped_count} already bound")
        generated = "; ".join(generated_parts)
        # `ReviewRow.detail` is ONE (heading, body) pair, so the two tables
        # share a section rather than each claiming one.
        bodies = [b for b in (stage_table(plan), accused_table(plan.rows)) if b]
        detail = (("Stages and defendants", "\n\n".join(bodies))
                  if bodies else ())
        stored = ((case_detail.get("dates") or {}).get("stages") or [])
        before = (f"{len(stored)} stage(s), "
                  f"{len(case_detail.get('entities') or [])} bind(s)")
        note = "; ".join(plan.skips)

        # Recorded in EACH terminal branch below with that branch's own
        # outcome, not `plan.status` -- which stays "would-patch" here
        # regardless of whether the case is applied or held back by --dry-run.
        if plan.status == "nothing-to-do":
            # The one TERMINAL event this path gets. Without it the case ends
            # on `ok`-statused intermediates only and vanishes from the ledger,
            # which cannot be told apart from a run that crashed before
            # reaching it.
            stage_note = ("the stage write was REFUSED on an incomplete read"
                          if any(_NO_DATES_KEY_MARKER in s for s in plan.skips)
                          else "the first-instance stage already matches the "
                               "court record")
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                      step="idempotency", status="already",
                      detail=f"nothing written: {stage_note}, and "
                             f"{resolved_count + skipped_count} court-record "
                             "defendant(s) are already bound")
            review.add(ReviewRow(slug=slug, status="nothing-to-do", before=before,
                                 generated=generated, note=note,
                                 detail=detail))
            stats["nothing-to-do"] = stats.get("nothing-to-do", 0) + 1
            continue
        if args.dry_run:
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                      step="patch", status="dry_run", detail=generated)
            review.add(ReviewRow(slug=slug, status="would-patch", before=before,
                                 generated=generated, note=note,
                                 detail=detail))
            stats["would-patch"] = stats.get("would-patch", 0) + 1
            continue
        try:
            apply_plan(api, plan)
        except Exception as exc:  # noqa: BLE001 - a 412 or a 400 costs this case only
            # `isinstance(exc, HTTPError) and exc.code == 412`, never a
            # substring test on the message: `apply_plan`'s own no-ETag
            # `ValueError` interpolates `plan.slug`, so a case slugged
            # `...-cr-0412` hitting that permanent refusal would otherwise be
            # logged `etag_conflict` -- telling an operator to retry a write
            # that will refuse again every time.
            status = ("etag_conflict"
                      if isinstance(exc, urllib.error.HTTPError) and exc.code == 412
                      else "rejected")
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                      step="patch", status=status,
                      detail=f"{type(exc).__name__}: {exc}")
            review.add(ReviewRow(slug=slug, status=status, before=before,
                                 generated=generated, note=note,
                                 detail=detail))
            stats["error"] = stats.get("error", 0) + 1
            continue
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                  step="patch", status="applied", detail=generated)
        review.add(ReviewRow(slug=slug, status="patched", before=before,
                             generated=generated, note=note,
                             detail=detail))
        stats["patched"] = stats.get("patched", 0) + 1

    review.write()
    log_event(logger, events, run_id=run_id, stage=STAGE, slug="",
              step="defendant_totals", status="ok", detail=rung_summary(all_rows))
    log_run_footer(logger, stage=STAGE, stats=stats, duration_s=time.time() - started)
    print_summary(stats, args.dry_run, "court-record binder")
    print(f"  defendants : {rung_summary(all_rows)}")
    print(f"review file: {review.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
