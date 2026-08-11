#!/usr/bin/env python
"""Accused binds and case dates, read from the case's own NGM court record.

No source documents, and no Django at IMPORT (the guard test pins that) --
though `bootstrap()` does configure it at runtime on any run that holds a name,
for the one comparison call below. The court record states these facts rather
than inferring them: a defendant is a defendant because a charge sheet says so,
and a verdict date is a verdict date because the Special Court's docket says so.

ONE THING IS NOT IN THE COURT RECORD: whether two cases naming the same person
mean the same human being. Those rows carry a name and nothing else -- no
address on any of the 1,414 in the census -- so `held_names` holds such a name
and `casework.held_identity` asks the model to compare the cases' press
releases, which do state district and office. That is this stage's only model
call, it fires once per HELD name rather than per case, and an answer that is
not high-confidence leaves the name held exactly as before. `--no-held-compare`
turns it off, restoring a run that spends no tokens at all.

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
module matches on exact name equality within the `person` prefix, and creates a
NEW entity when there is no unique exact match. Where that works the failure
mode is a duplicate entity, which is a merge, not a defamation.

WHERE THE CREATION COLLIDES, NOTHING IS BOUND. A 409 on the create says the slug
this name yields is already TAKEN -- and by an entity the ladder just declined to
identify, because a unique exact match would have bound at rung 2 and never
reached the POST at all. The collision is therefore rung 2's own refusal
condition restated: this name is not unique to this person. So the 409 path binds
nothing. It reports `failed`, names the IRI that was taken, and leaves the case
for a human. Keeping the pre-POST IRI and binding it -- what this module did
until 2026-08-07 -- silently converts "I could not identify this person" into "I
identified this person", on exactly the common names the truncation veto was
written for. The cost is a bind this run does not make; the alternative is the
one error this platform must never make.

WHY IT NEVER WRITES `convicted`. `decision_type` sits on the CASE, not on each
defendant. `ठहर` on a 19-defendant case does not say who, and `आंशिक ठहर` means
some were convicted and some cleared. `सफाई` is a whole-case acquittal, so it
alone is distributed to each defendant -- and only ever corrects an unfairly
plain "Accused" label. `charged` is true by construction everywhere else: every
case in this corpus is a Special Court `-CR-` case, so CIAA filed a charge sheet.

THE HOLD IS SCOPED TO ONE RUN, AND TO THE ENRICHABLE STATES. `held_names` only
sees a name on 2+ cases INSIDE the current selection, so `--limit`, `--slug`,
`--court-case`, `--batch-csv`, or a narrow `--fiscal-year` can each shrink
that selection below the cases sharing a name -- silently disabling the hold
for exactly the pair it exists to catch. An unrestricted sweep does not fix
this the way it might seem to: `casework.common.select.select_cases` filters
bulk selection to `ENRICHABLE_STATES` (DRAFT, IN_REVIEW), so a name already
bound as accused on a PUBLISHED case is invisible to the index by
construction, not by narrowing -- and PUBLISHED is exactly where a confirmed
bind already lives. Widening the index past that state gate is a
cost/coverage call for a human, not something this module defaults to. The
run log states both limits every run (`step="held_index"`) and warns
separately when the selection looks narrowed or a pass-1 read shrank it.

Usage:
    uv run python -m casework.enrich_court_record --dry-run --verbose
"""

import argparse
import json
import logging
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
from casework.common.llm import bootstrap, tier_for
from casework.common.review import ReviewRow, build_review_file, md_cell
from casework.common.select import ENRICHABLE_STATES, select_for_run
from casework.court_record import (
    BINDABLE_CODES,
    case_number_code,
    court_record_for_case,
    is_defendant,
    party_legal_name,
    party_name,
    split_alias,
)
from casework.entity_identity import entity_slug, prefix_is_creatable
from casework.entity_resolver import normalise_name
from casework.held_identity import (
    HeldVerdict,
    case_identity,
    compare_held,
    splittable,
)
from casework.held_identity import discriminator as held_discriminator
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


def run_entity_key(name, address, scope=""):
    """The `run_entities` key for one court-record party row.

    `scope` is the CASE SLUG, and is set only for a name a `different` held
    verdict split. That verdict's whole content is "these two are not the same
    person", so the cross-case reuse this map exists to perform is exactly
    wrong for it, and the key must not let the second case find the first's
    entry.

    The case slug, not the discriminator: two split cases bound to the same
    single district produce the SAME discriminator, so keying on that shares
    the entry again and hands case B case A's entity -- reintroducing the merge
    through the run cache, with the create's 409 guard never reached.
    `held_identity.splittable` refuses that verdict up front; this keying is the
    second line.

    NAME PLUS ADDRESS, never the bare name. `run_entities` is shared across
    every case in the run so that one person named on two cases becomes ONE
    entity rather than two. Keyed on the name alone that reuse is
    indiscriminate: two DIFFERENT people who merely share a name, each a
    defendant on a different case, collapse into a single entity, and case A's
    person then carries case B's accusation -- the same wrong-person bind the
    match rungs refuse, arriving through the create rung instead.

    `address` is the only other identifying column NGM stores on a party
    (`courts.serializers`'s `CaseEntitySerializer` exposes `side`, `name`,
    `address`, `nes_id` and nothing else), and it is what the charge sheet uses
    to tell namesakes apart, so it is what separates them here.

    EMPTY-TOLERANT, and the residual hole is deliberate: a row with no address
    keys on `(name, "")` and still reuses across cases, so two same-named,
    address-less defendants on two cases can still collapse. Closing that would
    mean keying on the case, which defeats the cross-case reuse this map exists
    for and mints a duplicate entity for every case a person appears on. Both
    halves go through `normalise_name` so a spacing or punctuation difference in
    the portal's transcription does not split one person into two entities.
    """
    return normalise_name(name), normalise_name(address or ""), scope


def resolve_defendant(api, name, row_nes_id, *, citation, live_prefixes,
                      run_entities, dry_run, address="", discriminator="",
                      distinct=False, scope=""):
    """Turn one court-record defendant name into an NES entity id.

    The ladder, top to bottom:
      1. the court row's own `nes_id`  -- a pure copy, no judgment
      2. exactly one person entity with that identical name
      3. create the entity from the court record

    `distinct` SKIPS rung 2, and is set only for a name a `different` held
    verdict split. Rung 2 binds the single existing person carrying this name,
    and the verdict has just said the cases name two people -- so at most one
    of them is that entity and nothing here can say which. Matching would give
    both cases the same IRI, which is the merge the split exists to prevent.
    `discriminator` then separates their created slugs, and `scope` (the case
    slug) keeps the run cache from sharing one entity between them.

    `run_entities` maps a `run_entity_key` (name AND address) to an IRI already
    created THIS RUN, and is shared across cases on purpose: without it, two
    cases naming the same defendant create two entities. Nothing here raises --
    a name that cannot become an entity is reported and the case keeps its other
    defendants. That covers the search read too: one transient 502 on one of a
    case's several defendant rows costs that row, not the run.
    """
    row_nes_id = (row_nes_id or "").strip()
    if row_nes_id and is_valid_entity_iri(row_nes_id):
        # `_is_person` too, not just a well-formed IRI: rungs 2 and 3 can only
        # ever produce a `person`, so without this rung 1 is the one way a
        # non-person IRI reaches an `accused` bind -- an office or a company
        # named as the accused individual. Not dead: this cohort carries no
        # `nes_id` at all, but `special/080-cr-0111` was backfilled with 185.
        if not _is_person(row_nes_id):
            return Resolution("", "failed",
                              f"the court row's nes_id {row_nes_id} is not a "
                              f"{PERSON_PREFIX} entity")
        return Resolution(row_nes_id, "nes_id")

    if distinct:
        why = ("a held verdict split this name across cases, so the one "
               "existing entity carrying it cannot be assumed to be this "
               "defendant")
    else:
        try:
            matched, why = exact_person_match(api, name)
        except Exception as exc:  # noqa: BLE001 - one bad search costs this name, not the case
            return Resolution("", "failed",
                              f"could not search for a match ({type(exc).__name__})")
        if matched:
            return Resolution(matched, "exact")

    key = run_entity_key(name, address, scope)
    if key in run_entities:
        return Resolution(run_entities[key], "created", "reused from this run")

    if live_prefixes is None:
        # NOT a judgement on `person` -- nothing was checked. `read_live_prefixes`
        # returns None for exactly this case, but `prefix_is_creatable` folds
        # None and [] to the same empty set, so without this branch one transient
        # 502 at run start reports every defendant on all 307 cases as needing a
        # prefix that is not creatable. That sentence is false for a prefix as
        # ordinary as `person`, and the dates still PATCH, so a re-run finds them
        # populated and the missing binds look deliberate.
        # `enrich_related_entities._cannot_create` draws the same distinction.
        return Resolution("", "failed",
                          f"{why}; the live entity prefix list could not be "
                          f"read, so {PERSON_PREFIX!r} was never checked -- "
                          "retry this case")
    if not prefix_is_creatable(PERSON_PREFIX, live_prefixes):
        return Resolution("", "failed", f"{why}; the person prefix is not creatable")
    slug = entity_slug(name)
    if not slug:
        return Resolution("", "failed", f"{why}; the name cannot be slugged")
    if distinct:
        # Without a discriminator both split cases derive the SAME slug, so the
        # second create 409s and binds nothing -- safe, but it reports a
        # collision where the real problem is that the case carries no fact to
        # name its own person by. Said plainly instead.
        if not discriminator:
            return Resolution("", "failed",
                              f"{why}; and the case carries neither a single "
                              "district nor a court case number to separate "
                              "this defendant's entity from the namesake's")
        slug = f"{slug}-{discriminator}"

    iri = build_entity_iri(PERSON_PREFIX, slug)
    if dry_run:
        # POST nothing, and report the IRI an --apply run would use IF
        # nothing already owns this slug. That "if" is real: a dry run never
        # reaches the `EntityAlreadyExists` handler below, so on the COMMON
        # path for a common name -- this slug already belongs to a person
        # the ladder declined to identify -- --apply refuses the bind this
        # row reports as made. The review file is approved BEFORE --apply, so
        # the reason says that plainly rather than implying the printed
        # patch is guaranteed to be the one sent.
        run_entities[key] = iri
        return Resolution(iri, "created",
                          "would create -- if this slug already belongs to "
                          "another entity, --apply refuses the bind instead "
                          "of using this IRI (a dry run has no network read "
                          "to check that here)")

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
        # BINDS NOTHING. A 409 says the slug is taken -- by an entity this
        # ladder just declined to identify, since a unique exact match would
        # have bound at rung 2 and never reached this POST. The collision is
        # therefore rung 2's own refusal restated (this name is not unique to
        # this person), and the pre-POST IRI names whoever already owns the
        # slug, not necessarily this defendant. `search_entities` marks
        # `complete=False` for any name whose results fill a page on relevance,
        # so this is the COMMON path for exactly the common names the
        # truncation veto was written for -- 13 rows carry `संजय प्रसाद यादव`,
        # and one of them owns the slug. Binding it turns "I could not identify
        # this person" into "I identified this person"; report it instead and
        # let a human decide.
        return Resolution("", "failed",
                          f"{why}; creating it collided with the existing "
                          f"{iri}, so this name is not unique to this person "
                          "-- refusing to bind an entity this run did not "
                          "identify")
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


def defendant_name_index(records_by_slug):
    """`{normalised name: {slug, ...}}` over every case selected for the run.

    Only records passing `BINDABLE_CODES` feed this index, matching
    `_accused_binds`'s own filter -- a ministry named on two `OA` cases must
    not consume a review slot for a name that was never a bind candidate.
    """
    index = {}
    for slug, records in records_by_slug.items():
        for record in records:
            if case_number_code(record["number"]) not in BINDABLE_CODES:
                continue
            for party in record.get("parties") or ():
                if not is_defendant(party):
                    continue
                # The LEGAL name, so the index keys on the same string
                # `_accused_binds` resolves and binds. Keying on the raw record
                # string would put `आवास भन्ने आभाश अर्याल` and a second case's
                # plain `आभाश अर्याल` in different buckets -- one man, two keys,
                # and the hold that exists to catch exactly that never fires.
                name = party_legal_name(party)
                if not name:
                    continue
                index.setdefault(normalise_name(name), set()).add(slug)
    return {name: frozenset(slugs) for name, slugs in index.items()}


def held_names(index):
    """`{normalised name: frozenset(slugs)}` for names on more than one case -- never auto-bound."""
    return {name: slugs for name, slugs in index.items() if len(slugs) > 1}


def bindable_defendants(records):
    """Defendant names on this case's bindable references, order preserved.

    The same `BINDABLE_CODES`/`is_defendant`/`party_legal_name` filter
    `defendant_name_index` applies, over records already in hand. Feeds the
    identity cards, so a name that could never be held never costs a
    `description` scan either.
    """
    names, seen = [], set()
    for record in records:
        if case_number_code(record["number"]) not in BINDABLE_CODES:
            continue
        for party in record.get("parties") or ():
            if not is_defendant(party):
                continue
            name = party_legal_name(party)
            key = normalise_name(name)
            if not name or key in seen:
                continue
            seen.add(key)
            names.append(name)
    return names


def _held_outcome(slugs, case_slug, verdict, identity):
    """`(row_reason, distinct, discriminator, address_override)` for a held name.

    Returns `row_reason` non-empty when the name stays HELD -- the caller emits
    the report row and binds nothing. Otherwise the three write parameters that
    an actionable verdict earns.

    A `same` verdict returns `address_override=""` so both cases key the same
    `run_entities` entry and share one created entity. The address is what
    `run_entity_key` normally uses to keep namesakes apart, and this verdict has
    replaced it as the thing establishing identity -- without the override, one
    case carrying an address and the other not would key differently and mint
    two entities for the person a `same` verdict just merged.
    """
    others = sorted(s for s in slugs if s != case_slug)
    shared = "also names a defendant on " + ", ".join(others)
    if verdict is None:
        return f"{shared} -- held for a human to rule on", False, "", None
    stated = f"{verdict.verdict}/{verdict.confidence or 'no confidence'}"
    if not verdict.is_actionable:
        why = "the model did not answer" if verdict.failed else stated
        return (f"{shared} -- held for a human to rule on ({why}: "
                f"{verdict.evidence})"), False, "", None
    if verdict.verdict == "different":
        return "", True, held_discriminator(identity) if identity else "", None
    return "", False, "", ""


def _accused_binds(api, case, records, *, live_prefixes, run_entities, dry_run,
                   held, decisions=None, identity=None):
    """`(items, rows, skips)` -- binds, a report row each, and non-prosecution skips.

    De-duplicated by name across every court reference on the case, order
    preserved, exactly like `defendant_names` does -- through the SAME
    `is_defendant`/`party_name` pair that module exposes, so the two paths
    cannot drift on who counts as a defendant. The whole party row is needed
    here (its `nes_id` for ladder rung 1, its `address` for the run-entity key),
    which is why this reads the parties itself rather than calling
    `defendant_names`.

    A held name (see `held_names`) is never told apart from a genuine match by
    anything this function alone can see. `decisions` -- `{normalised name:
    HeldVerdict}` from `held_identity.compare_held` -- is what can, and an
    absent or non-actionable verdict leaves the name held exactly as before.
    """
    outcome = bind_outcome(records)
    # The first BINDABLE record, never `records[0]`: a rejected reference
    # (an `OA`/`RE`/writ) never names these defendants, so citing it would
    # put a false provenance claim on the NES entity this creates.
    bindable = [r for r in records if case_number_code(r["number"]) in BINDABLE_CODES]
    citation = (bindable[0].get("detail") or {}).get("material_id", "") if bindable else ""
    items, rows, skips, seen = [], [], [], set()
    for record in records:
        code = case_number_code(record["number"])
        if code not in BINDABLE_CODES:
            skips.append(
                "skipping accused bind for court reference "
                f"{record['court']}/{record['number']}: case type {code!r} "
                "is not a prosecution")
            continue
        for party in record.get("parties") or ():
            if not is_defendant(party):
                continue
            # The alias prefix is stripped before ANY of this: the name that
            # resolves, creates the entity, keys the dedup and keys the held
            # index must be the one the index itself was built from.
            name, aliases = split_alias(party_name(party))
            key = normalise_name(name)
            if not name or key in seen:
                continue
            seen.add(key)
            other_slugs = held.get(key)
            distinct, disc, address = False, "", party.get("address")
            settled = ""
            if other_slugs is not None:
                verdict = (decisions or {}).get(key)
                reason, distinct, disc, override = _held_outcome(
                    other_slugs, case.get("slug"), verdict, identity)
                if reason:
                    rows.append({
                        "slug": case.get("slug"), "name": name, "how": "held",
                        "nes_id": "", "outcome": outcome, "reason": reason,
                        "aliases": aliases,
                        "court_case": f"{record['court']}/{record['number']}"})
                    continue
                if override is not None:
                    address = override
                settled = (f"held verdict {verdict.verdict}/{verdict.confidence}"
                           f": {verdict.evidence}")
            got = resolve_defendant(
                api, name, party.get("nes_id"), citation=citation,
                live_prefixes=live_prefixes, run_entities=run_entities,
                dry_run=dry_run, address=address, discriminator=disc,
                distinct=distinct,
                scope=case.get("slug") or "" if distinct else "")
            row = {"slug": case.get("slug"), "name": name, "how": got.how,
                   "nes_id": got.nes_id, "outcome": outcome, "aliases": aliases,
                   "reason": "; ".join(p for p in (settled, got.reason) if p),
                   "court_case": f"{record['court']}/{record['number']}"}
            rows.append(row)
            if not got.nes_id:
                continue
            # The stripped alias rides on the bind, not the entity name: the
            # court called this person something, and dropping that on the floor
            # loses the only place the record says so.
            note = f"प्रतिवादी — विशेष अदालत मुद्दा {record['number']}"
            if aliases:
                note += f"; अदालतको अभिलेखमा: {' भन्ने '.join([*aliases, name])}"
            item = {"nes_id": got.nes_id, "relationship_type": "accused",
                    "outcome": outcome, "notes": note}
            try:
                items.append(validate_bind_item(item))
            except ValueError as exc:
                row.update(how="failed", reason=str(exc))
    return items, rows, skips


def plan_case(api, case, etag, *, live_prefixes, run_entities, dry_run, held,
              court_record=None, decisions=None, identity=None):
    """Build the write for one case; writes nothing.

    Reads the court record itself unless `court_record` -- a pass-1
    `(records, skips)` pair -- is supplied, so a run planning many cases can
    read every court reference once and reuse it here.
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
        # silently deletes every bind the case actually has. Refused outright,
        # matching `enrich_related_entities.plan_case_entities`'s own guard for
        # the identical hazard.
        return CasePlan(slug, "no-entities-key",
                        skips=["case payload has no 'entities' key -- absent "
                               "is not empty; refusing to plan a write from "
                               "an incomplete read"])

    if court_record is not None:
        records, skips = court_record
    else:
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

    items, rows, accused_skips = _accused_binds(
        api, case, records, live_prefixes=live_prefixes,
        run_entities=run_entities, dry_run=dry_run, held=held,
        decisions=decisions, identity=identity)
    skips.extend(accused_skips)

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


#: Prefix `_accused_binds` puts on every record it skips for a non-prosecution
#: case type. Mirrors `_COURT_READ_FAILURE_PREFIX`'s role: the reference read
#: fine and was refused by policy, which is neither an unreadable reference
#: nor a fact about date derivation, so `_log_plan` routes it to `bind_plan`.
_NON_PROSECUTION_SKIP_PREFIX = "skipping accused bind for "


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
_RUNG_WORDS = {"nes_id": "nes_id_copied", "exact": "exact_match",
               "created": "created", "failed": "failed", "held": "held"}

#: Short label per rung for the tally lines, in print order. Insertion order is
#: the display order, so a case's line and the run footer read the same way.
_RUNG_LABELS = {"nes_id_copied": "copied", "exact_match": "matched",
                "created": "created", "held": "held", "failed": "failed"}


def rung_summary(rows):
    """`"5 defendant(s): 0 copied, 1 matched, 4 created, 0 held, 0 failed"`.

    EVERY rung prints, including its zero. `0 matched` is the load-bearing
    number in this corpus: 142 of 142 defendants in the 25-case run were
    CREATED, not matched, which is what tells an operator to expect duplicate
    entities rather than reuse of existing ones. A tally that dropped its zeroes
    would hide exactly the number worth reading.

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
    """`(resolved_count, held_count)` over a plan's per-defendant rows.

    Resolved means the ladder actually named an entity -- `how` in
    `{"nes_id", "exact", "created"}` -- so a `"failed"` row (an unslugabble
    name, a search error, a slug collision) counts as neither resolved nor
    held. `len(rows) - held_count` alone still counts a failed row as
    resolved, which is where "accused+N"/"N defendant(s) resolved" picked up
    a phantom bind: a name a run never turned into an entity read the same
    as one it did.
    """
    held = sum(1 for row in rows if row["how"] == "held")
    resolved = sum(1 for row in rows if row["how"] not in ("held", "failed"))
    return resolved, held


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
    """Emit the per-step events for one planned case. Returns `(held, resolved)`.

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
    resolved_count, held_count = _rung_counts(plan.rows)
    for row in plan.rows:
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
                  step="defendant_resolve", status="ok",
                  detail=f"{_RUNG_WORDS[row['how']]}: {row['name']} -> "
                         f"{_resolve_detail(row)}")
    if plan.fields:
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
                  step="dates", status="ok",
                  detail="proposed " + ", ".join(f"{k}={v}" for k, v in plan.fields))
    code_skips = 0
    for skip in plan.skips:
        if skip.startswith(_COURT_READ_FAILURE_PREFIX):
            # A per-reference read failure, not a date fact: `plan.status` is
            # not one of the SKIP statuses here (this case had at least one
            # readable reference, or `plan_case` would have returned
            # "no-court-reference" and `_log_plan` would never run), so the
            # earlier `court_read`/`ok` event already logged for this case
            # stands -- this event says the SAME court read was only partial,
            # which is an annotation on that read rather than this case's
            # outcome.
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
                      step="court_read", status="ok", detail=f"unreadable: {skip}")
            continue
        if skip.startswith(_NON_PROSECUTION_SKIP_PREFIX):
            # Read fine, refused by policy -- not a date fact either, so this
            # rides under `bind_plan` (see `_NON_PROSECUTION_SKIP_PREFIX`).
            code_skips += 1
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
                      step="bind_plan", status="ok", detail=skip)
            continue
        kind = "skip_open_case" if "not every court reference" in skip else "no_source"
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
                  step="dates", status="ok", detail=f"{kind}: {skip}")
    # "resolved", not "on the court record": when `code_skips` is non-zero the
    # court record named at least one defendant this run declined to look at,
    # and the earlier count must not be read as "the record named none". A
    # held row and a failed row both sit in `plan.rows` too but neither was
    # resolved (see `_rung_counts`), so both are excluded here.
    summary = (f"{'merged' if plan.entities is not None else 'no_additions'}: "
               f"{resolved_count} defendant(s) resolved"
               f" [{rung_summary(plan.rows)}]")
    if code_skips:
        summary += f"; {code_skips} court reference(s) skipped as non-prosecution"
    if held_count:
        summary += f"; {held_count} name(s) held for review"
    log_event(logger, events, run_id=run_id, stage=STAGE, slug=plan.slug,
              step="bind_plan", status="ok", detail=summary)
    return held_count, resolved_count


def _verdict_report(verdict):
    """One held verdict as JSON, or None when the name was never compared."""
    if verdict is None:
        return None
    return {"verdict": verdict.verdict, "confidence": verdict.confidence,
            "evidence": verdict.evidence, "per_case": verdict.per_case,
            "acted_on": verdict.is_actionable,
            "model_answered": not verdict.failed}


def _held_report(held, court_records, verdicts=None):
    """One entry per held name: its cases, the rows behind it, and any verdict.

    Recomputed from `court_records` (the pass-1 cache) rather than collected
    from `plan.rows` -- a case that never reaches `_accused_binds` (wrong
    state, no `entities` key) still needs to show up here, and re-applies
    `defendant_name_index`'s own `BINDABLE_CODES` filter since `court_records`
    holds every reference read, not just the bindable ones.

    A name with an ACTED-ON verdict stays in this file. It is no longer waiting
    on a human, but it is the record of a merge or a split this run performed on
    the model's word, which is exactly what a reviewer needs to be able to find
    afterwards -- `acted_on` tells the two apart.
    """
    report = []
    for name, slugs in sorted(held.items()):
        rows = []
        for slug in sorted(slugs):
            # `slug` came from `held`, which was built from `court_records`
            # itself (see `main`) -- a miss here is a real bug, not a case
            # this function should quietly render as having no rows.
            records, _ = court_records[slug]
            for record in records:
                if case_number_code(record["number"]) not in BINDABLE_CODES:
                    continue
                for party in record.get("parties") or ():
                    if not is_defendant(party):
                        continue
                    if normalise_name(party_name(party)) != name:
                        continue
                    rows.append({"slug": slug,
                                "court_case": f"{record['court']}/{record['number']}",
                                "name": party_name(party)})
        report.append({"name": name, "cases": sorted(slugs), "rows": rows,
                       "comparison": _verdict_report((verdicts or {}).get(name))})
    return report


def write_held_file(path, held, court_records, *, run_id, verdicts=None):
    """Write the held-names file beside the review file.

    Devanagari unescaped (`ensure_ascii=False`), matching every other
    casework output file -- an escaped `\\u0915` cannot be reviewed.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"run_id": run_id,
               "held": _held_report(held, court_records, verdicts)}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main(argv=None):
    parser = add_common_args(argparse.ArgumentParser(
        description="Bind court-record defendants and fill the case date fields."))
    parser.add_argument(
        "--no-held-compare", action="store_true",
        help="Do not ask the model whether two same-named defendants are one "
             "person; leave every held name for a human. Restores the "
             "pre-comparison behaviour, and makes the run spend no tokens.")
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

    # Pass 1: read every selected case's court record before planning any of
    # them, so the held index sees every defendant in the run -- a per-case
    # index would hold a name on case A and bind it on case B, the exact
    # collapse this split exists to close. A read failure here costs only
    # that case; it is dropped from `readable_cases` before pass 2 runs.
    court_records, readable_cases, identities = {}, [], {}
    # Dry runs only. Pass 2 re-reads each case for a FRESH ETag, but a dry run
    # never PATCHes, so that ETag is read and discarded -- one wasted request
    # per case, ~2,900 on a full-corpus dry run against a measured 4,470/hour
    # budget. Kept empty under --apply so the re-read there is unconditional.
    dry_run_details = {}
    for i, case in enumerate(cases, 1):
        slug = case.get("slug") or ""
        if not slug:
            # Two slug-less cases would both key `court_records` on the SAME
            # `""` -- pass 2 would then plan both against whichever record
            # set landed there last, one case's court record reaching the
            # other's `_accused_binds`. Refusing to index either closes that
            # collision rather than picking a loser between them.
            log_event(logger, events, run_id=run_id, stage=STAGE, slug="",
                      step="court_read", status="unreadable",
                      detail=f"pass 1: case {i} of {len(cases)} has no slug "
                             "-- cannot be read or indexed")
            stats["error"] = stats.get("error", 0) + 1
            continue
        try:
            case_detail, _ = api.get_case_with_etag(slug)
        except Exception as exc:  # noqa: BLE001 - one case's read failure is not the run's
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                      step="court_read", status="unreadable",
                      detail=f"pass 1: {type(exc).__name__}")
            stats["error"] = stats.get("error", 0) + 1
            continue
        readable_cases.append(case)
        if args.dry_run:
            dry_run_details[slug] = case_detail
        court_records[slug] = court_record_for_case(api, case_detail)
        # Built HERE, not after `held` is known, because this is the only
        # moment the run holds both the case payload and that case's defendant
        # names without a second read. The card keeps only short fields plus a
        # bounded excerpt per name, so retaining one per case costs far less
        # than retaining `case_detail` itself would.
        records_for_card, _ = court_records[slug]
        # BINDABLE references only, matching `_accused_binds`'s own "the first
        # BINDABLE record, never `records[0]`" rule: `discriminator` falls back
        # to `court_cases[0]`, and naming a permanent public entity IRI after a
        # reference this binder refuses to bind from would be a false claim
        # about where the person came from.
        identities[slug] = case_identity(
            case_detail, bindable_defendants(records_for_card),
            court_cases=[r["number"] for r in records_for_card
                         if case_number_code(r["number"]) in BINDABLE_CODES])
        # Pass 1 pays one HTTP round trip per case before any write happens,
        # so a full-corpus run is silent for hours without this -- the same
        # reason `CaseworkApi.iter_cases` narrates its own page fetches.
        logger.info("pass 1: read %d/%d selected cases (%s)", i, len(cases), slug)

    name_index = defendant_name_index(
        {slug: records for slug, (records, _) in court_records.items()})
    held = held_names(name_index)

    # A pass-1 failure shrinks the index by removing that case's defendants
    # from it entirely -- not just that case's own hold coverage, but any
    # OTHER case's protection for a name the two would have shared. There is
    # no in-run fix for that (the case's own data is simply unread), so this
    # is the visibility half: an operator can see readable < selected instead
    # of inferring it from `error=N` in the footer. The WARNING below fires
    # on EITHER cause of a small index -- a narrowed selection
    # (--limit/--slug/--court-case/--batch-csv/--fiscal-year) or a pass-1
    # read failure -- and names which one applied: an operator needs to tell
    # "I asked for a subset" apart from "a read failed" to know whether a
    # re-run would help.
    narrowed = bool(args.limit or args.slug or args.court_case or args.batch_csv
                    or args.fiscal_year)
    shrunk = len(readable_cases) != len(cases)
    # The index is ALSO narrower than "every case", unconditionally: bulk
    # selection (`select_cases`) only ever returns `ENRICHABLE_STATES`, so a
    # PUBLISHED case -- exactly the ones already carrying a confirmed accused
    # bind -- is absent here regardless of any CLI flag. Stated every run,
    # not folded into the WARNING branch below: an unrestricted, unshrunk
    # sweep must not read as "the index sees everything".
    index_detail = (f"selected={len(cases)}, readable={len(readable_cases)}, "
                    f"names_in_index={len(name_index)}, held={len(held)} "
                    f"(covers only {'/'.join(ENRICHABLE_STATES)} cases -- a "
                    "name already bound on a PUBLISHED case is invisible to "
                    "this index)")
    reasons = []
    if narrowed:
        reasons.append("selection narrowed by --limit/--slug/--court-case/"
                       "--batch-csv/--fiscal-year")
    if shrunk:
        reasons.append(f"{len(cases) - len(readable_cases)} case(s) failed their "
                       "pass-1 read")
    if reasons:
        index_detail += ("; WARNING: " + " AND ".join(reasons) +
                         " -- the held index only covers readable, selected "
                         "cases, so a name shared with a case OUTSIDE it "
                         "will not be held")
    log_event(logger, events, run_id=run_id, stage=STAGE, slug="",
              step="held_index", status="ok", detail=index_detail,
              level=logging.WARNING if reasons else logging.INFO)

    # The stage's ONLY model calls: one per HELD name, between the two passes.
    # A run holding nothing spends no tokens and never imports the LLM stack --
    # which is still this stage's ordinary case, since only 80 of ~1,414
    # measured defendant rows carry a name that lands on two cases.
    verdicts, usage = {}, None
    # Every per-defendant row the run produces, so the footer can tally the
    # ladder across cases. `stats` cannot carry this: it counts CASES by status,
    # and mixing defendant counts into it would print `would-patch=5 created=142`
    # as though both were case outcomes.
    all_rows = []
    if held and not args.no_held_compare:
        try:
            bootstrap(args.provider, args.model)
            from llm.invoke import invoke_json
            from llm.usage import UsageAccumulator

            usage = UsageAccumulator()
        except Exception as exc:  # noqa: BLE001 - no model means every name stays held
            log_event(logger, events, run_id=run_id, stage=STAGE, slug="",
                      step="held_compare", status="unavailable",
                      detail=f"{type(exc).__name__}: {exc} -- every held name "
                             "stays held for a human",
                      level=logging.WARNING)
        else:
            def _log_verdict(name, slugs, verdict):
                log_event(logger, events, run_id=run_id, stage=STAGE, slug="",
                          step="held_compare",
                          status="ok" if not verdict.failed else "failed",
                          detail=(f"{name} on {', '.join(slugs)} -> "
                                  f"{verdict.verdict}/"
                                  f"{verdict.confidence or 'no confidence'}"
                                  f"{'' if verdict.is_actionable else ' (still held)'}"
                                  f": {verdict.evidence}"))

            logger.info("comparing %d held name(s) against their press releases",
                        len(held))
            verdicts = compare_held(held, identities, invoke_json,
                                    tier=tier_for(STAGE), usage=usage,
                                    on_verdict=_log_verdict)
            # A `different` verdict is only actionable if the cases can be told
            # apart in the IRI. Two cases bound to the same single district each
            # discriminate to that district, so both would derive one slug and
            # the split would land as the merge it was ordered to prevent.
            for name, verdict in list(verdicts.items()):
                if verdict.verdict != "different" or not verdict.is_actionable:
                    continue
                cards = [identities[s] for s in sorted(held[name])
                         if s in identities]
                if splittable(cards):
                    continue
                verdicts[name] = HeldVerdict(
                    "unclear", confidence=verdict.confidence,
                    per_case=verdict.per_case,
                    evidence=(f"{verdict.evidence} [downgraded: these cases "
                              "yield no distinct district or court case number, "
                              "so this run cannot name the two people apart]"))
                log_event(logger, events, run_id=run_id, stage=STAGE, slug="",
                          step="held_compare", status="ok",
                          detail=f"{name}: split refused -- no distinct "
                                 "discriminator across its cases; held for a human",
                          level=logging.WARNING)
            acted = sum(1 for v in verdicts.values() if v.is_actionable)
            stats["held_compared"] = len(verdicts)
            stats["held_settled"] = acted
            log_event(logger, events, run_id=run_id, stage=STAGE, slug="",
                      step="held_compare", status="ok",
                      detail=f"compared {len(verdicts)} held name(s); {acted} "
                             f"settled by the model, {len(verdicts) - acted} "
                             "still held for a human")

    # Pass 2: plan (and maybe write) every case against that SAME `held`
    # mapping. `get_case_with_etag` is re-read here for a FRESH ETag --
    # pass 1's is stale by the time this write would land, and a stale
    # If-Match raises 412. The court record itself does not need a second
    # read: it is cached from pass 1 and passed straight into `plan_case`.
    for case in readable_cases:
        slug = case.get("slug") or ""
        if slug in dry_run_details:
            # Dry run: reuse pass 1's read. The ETag is deliberately "" because
            # nothing here will send an If-Match -- `apply_plan` is unreachable
            # under --dry-run, and handing back a real ETag would imply this
            # path could write.
            case_detail, etag = dry_run_details[slug], ""
        else:
            try:
                case_detail, etag = api.get_case_with_etag(slug)
            except Exception as exc:  # noqa: BLE001 - one case's read failure is not the run's
                log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                          step="court_read", status="unreadable",
                          detail=f"{type(exc).__name__}")
                stats["error"] = stats.get("error", 0) + 1
                continue

        # `court_records[slug]`, never `.get`: every slug in `readable_cases`
        # was inserted into `court_records` in the same pass-1 iteration, so
        # a miss here is a real bug -- falling back to `.get(slug)` would
        # silently re-read a fresh, un-indexed court record and plan off it
        # with no hold protection at all.
        plan = plan_case(api, case_detail, etag, live_prefixes=live_prefixes,
                         run_entities=run_entities, dry_run=args.dry_run,
                         held=held, court_record=court_records[slug],
                         decisions=verdicts, identity=identities.get(slug))
        # `detail=` carries `plan.skips` even on a clean "selected": a case
        # can reach "would-patch"/"nothing-to-do" with a partially-unreadable
        # court record (some references 404, at least one did not), and the
        # SAME line then tells an operator replaying the ledger what a bare
        # "skip_state"/"skip_no_court_ref" status alone cannot -- WHICH state,
        # WHICH missing/unreadable reference. Without this, "no-court-reference"
        # (a case naming none at all) and "every reference on this case
        # 404'd" produced an identical events-file line.
        # `"ok"` -- not `"selected"` -- for a case that proceeds: selection is
        # an intermediate step, and any status outside
        # `casework.ledger.NON_OUTCOME_STATUSES` is recorded as the case's
        # outcome for this stage. The three SKIP statuses keep their own
        # spellings because they ARE the outcome: nothing follows them.
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                  step="select", status=_SKIP_SELECT_STATUS.get(plan.status, "ok"),
                  detail="; ".join(plan.skips))
        if plan.status in _SKIP_SELECT_STATUS:
            stats[plan.status] = stats.get(plan.status, 0) + 1
            continue
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                  step="court_read", status="ok",
                  detail=court_read_summary(court_records[slug][0]))
        held_count, resolved_count = _log_plan(logger, events, run_id, plan)
        all_rows.extend(plan.rows)

        generated_parts = [f"{k}={v}" for k, v in plan.fields]
        if plan.entities is not None:
            # The outcome rides on every accused bind this case writes, so a
            # reviewer reading only the summary line still sees which verdict is
            # being claimed. `plan.rows` all carry the same one -- `bind_outcome`
            # is per case, not per defendant.
            outcome = next((r["outcome"] for r in plan.rows if r["nes_id"]), "")
            generated_parts.append(f"accused+{resolved_count}"
                                   + (f" ({outcome})" if outcome else ""))
        if held_count:
            generated_parts.append(f"{held_count} name(s) held for review")
        generated = "; ".join(generated_parts)
        detail = ("Court-record defendants", accused_table(plan.rows))
        if not detail[1]:
            detail = ()
        before = (f"case_start_date={case_detail.get('case_start_date')}, "
                 f"case_end_date={case_detail.get('case_end_date')}, "
                 f"{len(case_detail.get('entities') or [])} bind(s)")
        note = "; ".join(plan.skips)

        # Recorded in EACH terminal branch below with that branch's own
        # outcome, not `plan.status` -- which stays "would-patch" here
        # regardless of whether the case is applied, held back by
        # --dry-run, or rejected.
        if plan.status == "nothing-to-do":
            # The one TERMINAL event this path gets. Without it the case ends on
            # `ok`-statused intermediates only and vanishes from the ledger
            # entirely, which cannot be told apart from a run that crashed
            # before reaching it. `already` is the vocabulary every sibling uses
            # for "the fields were populated before we got here"
            # (`step="idempotency", status="already"`), and it is what this is
            # -- UNLESS a held name is the reason nothing else happened: that
            # case still has a human decision outstanding, so it cannot read
            # `already` (a status `casework.ledger.NON_OUTCOME_STATUSES`
            # would record as this stage's completed outcome) without the
            # audit trail claiming the stage is finished when it isn't.
            # `held_for_review` is the honest, distinct status for that case.
            nothing_to_do_note = (
                "nothing to add: no empty date this run could fill, "
                f"and {resolved_count} court-record defendant(s) are "
                "already bound")
            if held_count:
                nothing_to_do_note += f"; {held_count} name(s) held for review"
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                      step="idempotency",
                      status="held_for_review" if held_count else "already",
                      detail=nothing_to_do_note)
            review.add(ReviewRow(slug=slug, status="nothing-to-do", before=before,
                                 generated=generated, note=note, detail=detail))
            stats["nothing-to-do"] = stats.get("nothing-to-do", 0) + 1
            continue
        if args.dry_run:
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                      step="patch", status="dry_run", detail=generated)
            review.add(ReviewRow(slug=slug, status="would-patch", before=before,
                                 generated=generated, note=note, detail=detail))
            stats["would-patch"] = stats.get("would-patch", 0) + 1
            continue
        try:
            apply_plan(api, plan)
        except Exception as exc:  # noqa: BLE001 - a 412 or a 400 costs this case only
            # `isinstance(exc, HTTPError) and exc.code == 412`, never a
            # substring test on the message: `apply_plan`'s own no-ETag
            # `ValueError` interpolates `plan.slug`, so a case slugged
            # `...-cr-0412` hitting that (permanent) refusal would otherwise
            # be logged `etag_conflict` -- telling an operator to re-read and
            # retry a write that will refuse again every time.
            status = ("etag_conflict"
                     if isinstance(exc, urllib.error.HTTPError) and exc.code == 412
                     else "rejected")
            log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                      step="patch", status=status,
                      detail=f"{type(exc).__name__}: {exc}")
            review.add(ReviewRow(slug=slug, status=status, before=before,
                                 generated=generated, note=note, detail=detail))
            stats["error"] = stats.get("error", 0) + 1
            continue
        log_event(logger, events, run_id=run_id, stage=STAGE, slug=slug,
                  step="patch", status="applied", detail=generated)
        review.add(ReviewRow(slug=slug, status="patched", before=before,
                             generated=generated, note=note, detail=detail))
        stats["patched"] = stats.get("patched", 0) + 1

    held_path = review.path.parent / (review.path.stem + ".held.json")
    write_held_file(held_path, held, court_records, run_id=run_id,
                    verdicts=verdicts)

    review.write()
    log_event(logger, events, run_id=run_id, stage=STAGE, slug="",
              step="defendant_totals", status="ok", detail=rung_summary(all_rows))
    log_run_footer(logger, stage=STAGE, stats=stats, duration_s=time.time() - started)
    print_summary(stats, args.dry_run, "court-record binder")
    print(f"  defendants : {rung_summary(all_rows)}")
    if usage is not None and usage.calls:
        # Recorded because this stage now spends tokens. Without it an 80-call
        # run and a 0-call run leave an identical footer, and every sibling
        # enricher reports its usage.
        #
        # `as_dict()["by_provider"]`, which is what `render_usage_table` takes
        # and what every sibling passes it (`enrich_missing_bigo`,
        # `enrich_tags`). NOT `totals()` -- that belongs to the OTHER
        # accumulator in `llm/usage.py`, so this line raised AttributeError and
        # took the run's exit code with it. It only ever ran when the held
        # comparison genuinely reached a provider, which is why every earlier
        # dry run (held_compare unavailable -> usage stays None) missed it.
        from llm.usage import render_usage_table
        print()
        print(render_usage_table(usage.as_dict()["by_provider"],
                                 title="held-name comparison"))
    print(f"review file: {review.path}")
    print(f"held-names file: {held_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
