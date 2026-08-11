#!/usr/bin/env python
"""Extract CIAA Special Court case entities via LLM, then resolve each extracted
name to an existing NES entity and bind it into the section it was extracted under
(accused, alleged, related, witness, location, ...).

Ported from the deleted `casework/enrich_related_entities.py` (recovered at
donor commit `0321a85`, 553 lines). Reads a case's press-release AND/OR
court-order source text entirely over the Jawafdehi HTTP API and asks the
premium LLM tier to extract related/location entities plus short accused-person
notes, in one response.

THE DONOR'S WRITE SHAPE (0321a85, no longer valid against this branch):
    entity_id = api.create_entity(display_name=name, nes_id="")        # donor line 510
    entities_to_patch.append({"entity": entity_id,
                               "relationship_type": rel_type_enum,
                               "notes": notes})                        # donor lines 522-528
    api.patch_field(slug, "entities", entities_to_patch)                # donor line 543
The donor blindly minted a brand-new entity for every LLM-extracted name, with
NO NES resolution at all -- keyed by a flat `entity` id, no `nes_id`, no
`outcome`. `CaseworkApi.create_entity` does not exist on this branch and never
has during this porting project, and it still does not exist: entities are
owned by NES and must already exist there before this module can bind one --
an unmatched name is reported for human review or in the no-match file, never
minted.

THE CURRENT SCHEMA (`cases/caseworker_serializers.py::EntityPatchItemSerializer`)
requires every `/entities` item to be
    {"nes_id": <canonical NES @id IRI, validated by is_valid_entity_iri>,
     "relationship_type": ..., "outcome"?: <ACCUSED-role only>, "notes": ...}
"The bind holds the canonical NES entity id directly; entities are owned by
NES and must already exist there (no display-name fallback)" (serializer
comment, verbatim).

WHAT THIS MODULE DOES: the deterministic resolver lives in
`casework/entity_resolver.py` -- matching an extracted name against NES search
candidates is entirely score-based, with `MIN_BIND_SCORE = 0.85`; there is no
fuzzy/edit-distance matching and no LLM call anywhere in the matching step.

BY DEFAULT EVERY NAME THAT MATCHED AN NES ENTITY IS BOUND, into the section its
own `relationship_type` names -- any of the nine `cases.models.RelationshipType`
accepts. When several entities tie above the threshold, the best-scoring one wins
by a deterministic `(-score, nes_id)` sort, so some binds WILL name the wrong
namesake. That is the accepted cost of the mode; every such bind is marked
`[UNCERTAIN]` on the console and carries a `promoted over:` reason in
`*.binds.jsonl`, which is how they are found again. Measured on the 142-row
labelled set: precision 0.872, recall 0.872, and all five wrong binds are
Election Commission candidate records rather than namesake mix-ups.

ONE REFUSAL SURVIVES THAT MODE:

* AN UNREADABLE ENTITY DOCUMENT (`apply_document_veto`'s fail-closed branch). One
  403 or 502 would otherwise bind whichever namesake sorted first with nothing
  having been checked at all.

A CROSS-SCRIPT-ONLY MATCH USED TO BE THE SECOND, and is now bound (2026-08-05:
this stage produces no review queue, and which entities are real is a later
pass). The risk it guarded is real and unmitigated: कमल थापा scores 0.96 against
a `Kamala Thapa` entity and 0.00 against कमला थापा, so the bind can name a woman
in a case charging a man. `resolve` still reports the veto, so the reason text
survives on the bind row in `*.binds.jsonl` -- that file is where such a bind is
found again.

`--strict` is the conservative pipeline: an ambiguity or a veto goes to the review
report instead of binding. Same labelled set: precision 1.000, recall 0.846.

Either way a name with NO NES candidate goes to the no-match report -- there is
nothing to choose between, and this module never creates an NES entity.

EVERY name comes from the extraction. `accused` is a section the LLM may use like
any other; it is NOT also read from the case's NGM court record. That path was
removed: it needs neither a document nor an LLM, so living here put it behind five
gates it has no use for -- the already-enriched skip, the MARKDOWN-role
prerequisite, the no-source gate, the empty-prompt gate, and an LLM failure. A case
with a complete court record and five named defendants bound none of them whenever
its press-release PDF lacked a MARKDOWN role, and a token-cap failure on
078-CR-0001 cost that case all five. `casework/court_record.py` is kept and fully
tested but unwired, pending a decision on giving it its own CLI.

`casework.enrich_related_entities.plan_case_entities`
builds a per-case write plan from the resolver's decisions and
`apply_entity_plan` executes it as a single conditional (`If-Match`) whole-list
replace of `/entities` -- never a partial patch, so an existing bind and its
notes are always preserved via `merge_entity_binds`. Writes are DRAFT-only
(`REQUIRED_WRITE_STATE`) and dry-run by default: `--dry-run` prints what WOULD
bind without writing anything; `--apply` is required to actually write, and
even then `CaseworkApi` itself refuses a non-loopback host unless
`--allow-remote-writes` is also passed -- never pass that against production.

Usage:
    uv run python -m casework.enrich_related_entities --dry-run
    uv run python -m casework.enrich_related_entities --slug case-0123
    uv run python -m casework.enrich_related_entities --limit 10 --verbose
    uv run python -m casework.enrich_related_entities --apply   # loopback only
"""

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

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
from casework.common.materials import source_chunks, source_text
from casework.entity_identity import entity_slug, prefix_is_creatable
from casework.common.parse import parse_extraction_response
from casework.common.pipeline import (
    COURT_TYPES,
    PRESS_TYPES,
    STAGES,
    RunReport,
    unmet_prerequisites,
)
from casework.common.review import md_cell
from casework.common.select import select_for_run
# `defendant_names` is deliberately NOT imported. Reading accused from the case's
# NGM court record was removed from this enricher: it needs no document and no LLM,
# so it does not belong behind this module's five document/LLM gates (a case with a
# perfect court record bound zero defendants whenever its press-release PDF lacked a
# MARKDOWN role, or the LLM call failed). `casework/court_record.py` and its tests
# are kept intact, unwired, pending a decision on giving it its own CLI.
from casework.entity_resolver import (
    BIND,
    MIN_BIND_SCORE,
    NO_MATCH,
    REVIEW,
    Decision,
    _name_vetoes,
    apply_document_veto,
    normalise_name,
    resolve,
)
from jawafdehi_shared.entities.ids import build_entity_iri, is_valid_entity_iri

log = logging.getLogger("casework.enrich_related_entities")

STAGE = STAGES["entities"]

# The output cap for one extraction. THE ONE PLACE this port deliberately departs
# from the donor's `max_tokens=2000`, and it is not a tuning preference -- 2000 is
# now too small to hold a valid response, so the call fails outright rather than
# returning less:
#
#   RuntimeError: claude_cli failed (rc=1): ... "API Error: Claude's response
#   exceeded the 2000 output token maximum."
#
# Reproduced on 078-CR-0001 with --model sonnet, a five-defendant case. Two things
# grew the response past the donor's cap: the extraction now asks for five sections
# rather than two (accused/alleged/witness were added so the binder's widened scope
# is reachable), and every name and note is Devanagari, which tokenises far worse
# than Latin -- roughly 2-3 tokens per character, so a dozen names with Nepali notes
# alone approaches 2000.
#
# 8000 matches `enrich_timeline.TIMELINE_MAX_TOKENS`, which carries the same
# fixed-constant treatment for the same reason. A cap costs nothing unless the model
# actually reaches it: billing is on tokens produced, not on the ceiling. There is
# no env knob because no other constant in `casework.common` has one.
EXTRACTION_MAX_TOKENS = 8000

# ── Slicing constants (verbatim from the donor's `env_int(NAME, default)`
# defaults). The donor read these via an `env_int()` helper that lived in the
# deleted `casework/common.py` and was never re-created in the Task 5-11
# common package (see `enrich_missing_bigo.py`'s identical note) -- fixed at
# the donor's own defaults.
COURT_ORDER_FULL_THRESHOLD = 8_000
COURT_ORDER_HEAD_CHARS = 4_000
COURT_ORDER_TAIL_CHARS = 2_000
COURT_ORDER_THAHAR_CHARS = 12_000

PRESS_RELEASE_CHARS = 3_000
PRESS_RELEASE_CHARS_NO_COURT = 18_000

PROMPT_HARD_MAX = 25_000

SYSTEM_PROMPT = """You are a Nepali legal research assistant helping to build a public transparency database of court cases.
Analyze the provided Nepali legal documents (press release and/or court order excerpts) and extract structured data.

You must extract THREE things in a single response:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 1 — LOCATION ENTITIES (relationship_type="location")
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Extract the district(s), municipality, or province WHERE THE CASE EVENTS occurred
or where the key assets/funds at issue are located.

STRICT RULES:
- Extract ONLY where the case events happened or where the assets are.
- DO NOT extract accused home addresses, birthplaces, or permanent addresses.
- DO NOT extract the location of courts or government inquiry offices.
- Extract 1 location for simple cases. Extract 2-3 only if the case genuinely spans
  multiple districts.
- The entity_name must be the PLACE NAME ALONE. Never combine it with an activity,
  an organisation, or anything else. The place is the entity; what happened there
  belongs in notes.
- Put the activity context in notes instead, in Nepali.

Examples of CORRECT location entities:
- "सुर्खेत"      notes: "साझा भण्डार सहकारीको कारोबार भएको जिल्ला"
- "जनकपुरधाम"    notes: "स्वास्थ्य उपकरण खरिद भएको स्थान"
- "सर्लाही"      notes: "भरत ताल निर्माण परियोजना रहेको जिल्ला"
- "खैरहनी नगरपालिका"  notes: "नापी कार्यालयको कारोबार भएको नगरपालिका"
- "काठमाडौं"     notes: "जग्गा तथा शेयर लगानी रहेको जिल्ला"

Examples of WRONG location names:
- "स्वास्थ्य उपकरण खरिद - जनकपुरधाम" ← an activity glued to a place, NEVER do this
- "घरजग्गा सम्पत्ति - काठमाडौं" ← a description of property, not a place
- "तनहुँ जिल्ला" ← accused home address, SKIP
- "काठमाडौं" ← if only reason is court/CIAA office, SKIP

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 2 — PEOPLE AND ORGANIZATIONS (relationship_type="related" unless stated)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Any person or organization connected to the case.
Extract ALL of these categories that appear in the documents:

  GOVERNMENT BODIES — ministry, department, municipality, office whose funds were
  misused or where the accused worked.
  Examples: "जलश्रोत तथा सिँचाइ विभाग"  notes: "आरोपी कार्यरत रहेको सरकारी निकाय"
            "राष्ट्रिय सूचना प्रविधि केन्द्र"  notes: "खरिद प्रक्रियामा संलग्न सरकारी निकाय"

  COMPANIES/CONTRACTORS — firms, JVs, cooperatives, suppliers, foreign companies.
  Examples: "कल्पवृक्ष-कोहिनूर जे.भी."  notes: "ठेक्का प्राप्त गर्ने संयुक्त उद्यम"
            "UOB Singapore बैंक"  notes: "Singapore स्थित बैंक, रकम हस्तान्तरणमा प्रयोग"

  FAMILY MEMBERS — spouse, children, relatives holding assets.
  Example: "श्रृजना गिरी"  notes: "आरोपितको श्रीमती, सम्पत्ति हस्तान्तरण गरिएको"

  CO-DEFENDANTS/ASSOCIATES — secondary actors, facilitators, middlemen.
  Example: "नानी काजी थापा"  notes: "घुस लेनदेनमा सहयोग"

  INVESTIGATING/PROSECUTING BODIES — DO NOT extract the inquiry commission
  (अख्तियार दुरुपयोग अनुसन्धान आयोग) or special attorney office as standalone
  entities — they are present in every case. DO NOT extract individual prosecutors,
  attorneys, judges, or court staff — they are performing standard professional
  duties, not materially connected to the case events.
  Only extract named CIAA investigation officers if they are specifically named
  and their investigation is directly relevant.
  Example: "रविन्द्र कुमार बुढाप्रिथी"  notes: "अनुसन्धान अधिकृत, CIAA"

  MEDIA — DO NOT extract a newspaper, portal or broadcaster whose only role was
  REPORTING the case. It is a source, not a participant.
  Example of what to SKIP: "नयाँ पत्रिका" (published the story that prompted the
  complaint). Extract a media organisation only when it is itself accused, owns
  assets at issue, or received the funds.

Notes must never be blank for related entities. Always describe the specific connection.
Only extract entities with CONFIRMED connections — not people who were later acquitted.

DO NOT EXTRACT THE DEFENDANTS. The people the charge sheet (आरोपपत्र) names are
already held in the court record and are read from there, not from this text.
Extracting them here would guess at names the court record states exactly.
Skip them entirely — do not list them under any relationship_type.

USE A MORE SPECIFIC relationship_type INSTEAD OF "related" when the documents make
the role plain. Only these two; when in doubt use "related".

  "alleged" — named as implicated in the documents, but NOT on the charge sheet.
  Example: "नानी काजी थापा"  notes: "घुस लेनदेनमा संलग्न भनी उल्लेख, अभियोग लगाइएको छैन"

  "witness" — a named inquiry officer or witness.
  Example: "रविन्द्र कुमार बुढाप्रिथी"  notes: "अनुसन्धान अधिकृत, CIAA"

PRIORITY ORDER: People and organizations DIRECTLY involved in the case events come first.
Generic legal infrastructure (courts, attorney offices) should be skipped unless a
specific named person from those bodies is materially connected.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PART 3 — ACCUSED NOTES (accused_notes array)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
For each primary accused person named in the documents, extract a SHORT note
describing their job title and role. Format: "job title, employer"
Examples:
  "तत्कालीन प्रबन्ध निर्देशक, नेपाल टेलिकम"
  "तत्कालीन नगरप्रमुख, खैरहनी नगरपालिका"
  "नापी अधिकृत, नापी कार्यालय चाबहिल"

Only include primary accused persons. Keep notes under 80 chars.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
OUTPUT FORMAT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Output ONLY this JSON object, no other text:
{
  "entities": [
    {
      "entity_name": "Name exactly as in document",
      "relationship_type": "location", "related", "alleged" or "witness",
      "entity_prefix": "the category from the list below",
      "entity_type": "Person", "Organization", "GovernmentOrganization" or "Place",
      "is_named_entity": true or false,
      "name_en": "the name in English, or \"\" if you cannot give one",
      "notes": "specific description"
    }
  ],
  "accused_notes": [
    {
      "name": "Accused person name exactly as in document",
      "notes": "job title, employer"
    }
  ]
}
"""

#: Appended to `SYSTEM_PROMPT` when `--create-entities` is on, carrying the live
#: category list. Only then: without the flag nothing is created, and asking for
#: two fields nobody reads would spend prompt budget on a case where the budget
#: is already the binding constraint (`PROMPT_HARD_MAX`).
#:
#: The list arrives from `GET /api/entity_prefixes` rather than being hardcoded,
#: because it is `SELECT DISTINCT prefix` over live entities and grows. A
#: hardcoded copy would silently refuse categories that exist.
PREFIX_PROMPT_TEMPLATE = """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ENTITY CATEGORY (entity_prefix)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Every entity needs a category. CHOOSE FROM THIS LIST ONLY -- a value not on it
is discarded and the entity is not created:

{prefixes}

Pick the most specific one that fits. A person is always `person`. A district
forest office is `organization/government/district/dfo`, not `organization`.
A district is `location/district`.

Set `entity_type` to match: `Person` for a person, `GovernmentOrganization` for
a state body, `Organization` for a company or NGO, `Place` for a location.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IS THIS A NAMED THING? (is_named_entity)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Set `is_named_entity` to true ONLY when the string names ONE SPECIFIC thing that
exists in the world and could be looked up in a register. Set it to false when
the string is a CATEGORY of thing, a description, or a phrase.

  true   "विध मानेजमेन्ट प्रा.लि."      one registered company
  true   "हेम राज विष्ट"                 one person
  true   "कर्मचारी सञ्चय कोष"            one named state fund
  false  "सामुदायिक वन उपभोक्ता समूह"   a KIND of group, not one named group
  false  "घरजग्गा सम्पत्ति"              a description of property
  false  "ठेक्का प्राप्त गर्ने कम्पनी"    a role, not a name

When false, the entity is still recorded against the case but no new register
entry is made for it. When you are unsure, answer false.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ENGLISH NAME (name_en)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Give the name in English. Many Nepali documents write English company names in
Devanagari -- convert those BACK to the English they came from rather than
spelling them out phonetically.

  "फरेष्ट डेभलपमेन्ट एण्ड इण्डष्ट्रिज"  ->  "Forest Development and Industries"
  "ग्लोवल वाइल्ड फार्मिङ प्रा.लि."      ->  "Global Wild Farming Pvt. Ltd."
  "कर्मचारी सञ्चय कोष"                  ->  "Employees Provident Fund"
  "हेम राज विष्ट"                        ->  "Hem Raj Bista"

Return "" if you genuinely cannot give one. Never guess a company's registered
English name when the document does not support it.
"""


def prefix_prompt_section(live_prefixes):
    """The category instructions for `SYSTEM_PROMPT`, or "" with no prefixes.

    Returns "" rather than a template with an empty list: an instruction to
    choose from nothing would make the model invent values, and every invented
    value is then discarded by `prefix_is_creatable` -- an expensive way to
    produce no entities.
    """
    if not live_prefixes:
        return ""
    listed = "\n".join(f"  {p}" for p in sorted(live_prefixes))
    return PREFIX_PROMPT_TEMPLATE.format(prefixes=listed)

# Writes are DRAFT-only. This is also what makes the merge safe: the case read
# BLANKS per-entity `notes` for non-casework viewers
# (`cases/serializers.py::get_entities`), so merging a redacted snapshot into a
# whole-list replace would wipe every existing note. `CaseViewSet.get_queryset`
# makes DRAFT retrieve casework-only -- if we can read the case at all, the notes
# we read are the real ones. IN_REVIEW is publicly retrievable, so it is NOT
# safe to merge even though `select.ENRICHABLE_STATES` allows it for extraction.
REQUIRED_WRITE_STATE = "DRAFT"

# Mirrors `cases.models.RelationshipType.values`. Hardcoded because this module
# is DB-free by design and must not import Django; a test asserts the two agree.
RELATIONSHIP_TYPES = (
    "alleged", "accused", "related", "witness", "opposition", "victim",
    "location", "respondent", "petitioner",
)

#: The section this enricher refuses outright. Defendants come from the NGM
#: court record (`casework/court_record.py::defendant_names`), which states them
#: instead of guessing, and an accused bind is the only one that may carry
#: `outcome`. Confirmed with Gaurav's supervisor on 2026-08-06.
ACCUSED_SECTION = "accused"

#: The one section that never creates. NES holds all 77 districts from a
#: gazetteer ingest, keyed by official code (`location/district/jhapa-np0104`),
#: so anything this stage would mint here is a duplicate or junk.
LOCATION_SECTION = "location"

#: Where a section the API rejects lands. `related` and not a guess at the
#: intended meaning: it is what the extraction prompt already defaults to
#: ("PART 2 -- PEOPLE AND ORGANIZATIONS (relationship_type=\"related\" unless
#: stated)"), so a coerced row claims exactly what an unstated one would.
DEFAULT_RELATIONSHIP_TYPE = "related"

#: `created.jsonl` outcomes that yielded an IRI the case can bind. The other two
#: -- `skipped` and `error` -- leave the name in `nomatch`.
CREATED_OUTCOMES = frozenset({"created", "would-create", "already-exists", "reused"})


def bind_relationship_type(entity):
    """One bind's relationship type, lowercased and trimmed, from either key.

    THE SINGLE PLACE that knows the case API is asymmetric about this field: a
    bind is WRITTEN as `relationship_type` but READ BACK as `type`, and
    `relationship_type` never appears on a read. Verified against 1,099
    production binds -- the read values are accused, related, location,
    respondent, petitioner and alleged, all under `type`.

    This exists as a chokepoint because getting it wrong once already cost a
    Critical: the pre-LLM skip checked `relationship_type` alone, so it matched
    nothing and every case re-spent a premium LLM call on every run. The fix for
    that bug hand-copied the tolerance to a second site rather than centralising
    it, which left the same trap open for a third. Read the field only through
    here, and a future third key name is a one-line change.

    Returns "" when neither key is present, so a caller must supply its own
    default rather than inherit a silent one.
    """
    if not isinstance(entity, dict):
        return ""
    return (entity.get("type") or entity.get("relationship_type") or "").strip().lower()


def current_entity_binds(case):
    """The case's existing binds in PATCH shape, order preserved.

    The read shape carries `type`, `display_name`, `entity_type` and `outcome`;
    the patch shape wants `relationship_type` and neither display field.
    `outcome` is deliberately DROPPED: an omitted outcome stays absent from the
    serializer's validated data, so the persist step preserves an accused bind's
    existing verdict rather than resetting it to 'charged'.
    """
    binds = []
    for entity in (case.get("entities") or []):
        nes_id = (entity.get("nes_id") or "").strip()
        if not nes_id:
            continue
        binds.append({
            "nes_id": nes_id,
            "relationship_type": bind_relationship_type(entity) or "related",
            "notes": entity.get("notes") or "",
        })
    return binds


def bind_key(item):
    """The identity of a bind: `(nes_id, relationship_type)`.

    NOT `nes_id` alone. The DB's uniqueness constraint is
    `unique_case_entity_relationship_type` over
    `("case", "nes_id", "relationship_type")` (`cases/models.py`), so one entity
    may legitimately hold two binds on the same case under different sections --
    an organisation that is both the `location` of the events and a `related`
    party, say. Keying on `nes_id` alone silently dropped the second one, which
    with the widened section scope is now a reachable case rather than a
    theoretical one.

    Idempotency is unaffected: a re-run produces the same pair, so it still
    matches and is still skipped.
    """
    return ((item.get("nes_id") or "").strip(),
            (item.get("relationship_type") or "").strip().lower())


def merge_entity_binds(current, additions):
    """Append each new bind not already present, preserving existing order.

    Never reorders, never drops, never overwrites an existing bind -- the
    whole-list replace makes any omission destructive, and an existing bind
    carries a human's notes. "Already present" means the same
    `(nes_id, relationship_type)` pair; see `bind_key`.
    """
    have = {bind_key(bind) for bind in current}
    merged = list(current)
    for item in additions:
        key = bind_key(item)
        if key in have:
            continue
        merged.append(item)
        have.add(key)
    return merged


def validate_bind_item(item):
    """Local mirror of `EntityPatchItemSerializer`'s rules, applied BEFORE the
    request body is built so a bad item never reaches the API. Raises ValueError.
    """
    nes_id = (item.get("nes_id") or "").strip()
    if not is_valid_entity_iri(nes_id):
        raise ValueError(
            f"not a canonical NES entity IRI: {nes_id!r} (want "
            "https://<authority>/entity/<prefix>/<slug>)")
    rel_type = item.get("relationship_type")
    if rel_type not in RELATIONSHIP_TYPES:
        raise ValueError(f"unknown relationship_type: {rel_type!r}")
    if item.get("outcome") and rel_type != "accused":
        raise ValueError(
            f"outcome {item['outcome']!r} is only legal on an 'accused' bind, "
            f"not {rel_type!r}")
    return item


def validate_new_bind(item):
    """`validate_bind_item` plus the one rule that applies only to NEW binds.

    Split deliberately. `apply_entity_plan` validates every row of the
    whole-list PATCH, and that list carries binds the case ALREADY has -- a
    human's accused bind, or one the court-record path wrote. Refusing accused
    there would make any such case unpatchable, which is the opposite of the
    intent: those binds are the authoritative ones.

    What this module may not do is PROPOSE an accused bind of its own.
    Defendants come from the NGM court record
    (`casework/court_record.py::defendant_names`), which states them rather
    than guessing. `plan_case_entities` drops the section before resolution, so
    this is the backstop for a caller assembling additions by hand.
    """
    validate_bind_item(item)
    if item.get("relationship_type") == ACCUSED_SECTION:
        raise ValueError(
            "this enricher does not propose 'accused' binds: defendants come "
            "from the NGM court record, not the LLM")
    return item


@dataclass
class EntityBindPlan:
    slug: str
    action: str  # WOULD_PATCH | NOOP | SKIP_STATE
    # Diagnostics. Nothing in this module reads either one -- they exist so a
    # caller inspecting a plan (a dry-run harness, a debugging session) can see
    # the state the decision was made against and how many binds the case
    # already carried. `bind_materials.BindPlan` DOES consume its equivalents,
    # so do not assume these are wired up here by analogy with it.
    state: str = ""
    if_match: str | None = None
    n_current: int = 0
    # The section rides on the row for the same reason it does on `review`: bind
    # identity is `(nes_id, relationship_type)`, so one entity can appear twice
    # here under different sections and a `nes_id`-keyed lookup collapses them --
    # both rows then report whichever section was written last.
    bound: list = field(default_factory=list)    # (name, Decision, notes, section)
    # The section is on the row because it belongs to the row: two extracted items
    # can name the same person in different sections, so it cannot be recovered
    # from the name afterwards. Carries the raw (lowercased) value for an
    # unrecognised section, which is exactly what a caseworker needs to see.
    review: list = field(default_factory=list)   # (name, Decision, section)
    # The section rides here for the same reason it does on `bound`/`review`: it
    # cannot be recovered from the name afterwards, and on a run where nothing
    # resolves this list is the ONLY record of what each name was said to be.
    nomatch: list = field(default_factory=list)  # (name, Decision, section)
    # Sections the extraction named that the case API will not accept, rewritten
    # to `related`. Recorded because a silently relabelled section is a section
    # nobody asserted -- the run log and the reports name the original.
    coerced: list = field(default_factory=list)  # (name, original, "related")
    # Names the create step made an entity for. They are REMOVED from `nomatch`
    # once created, so without this list `plan_summary` sees a name that produced
    # no row anywhere and counts it as already-bound -- which reported all 12
    # entities the first production dry run would have created as work that did
    # not need doing.
    created: list = field(default_factory=list)  # names
    #: (name, section) the extraction produced for a section this enricher does
    #: not own. Only `accused` today -- reported, never bound, never created.
    court_record_only: list = field(default_factory=list)
    patch_items: list = field(default_factory=list)
    reason: str = ""
    # There are no separate accused lists. Every name this planner handles comes
    # from `extracted_items` and lands in bound/review/nomatch, whatever section it
    # was extracted under -- including `accused`. The three court-record lists that
    # used to live here went with the court-record path.
    #
    # This is what keeps `plan_summary` honest: it derives `already_bound` by
    # subtracting those three lists from the EXTRACTED name count, which only works
    # while every name in them came from that count. A future source of names that
    # is NOT an extraction cannot reuse these lists for exactly that reason.
    #
    # True once the resolution loop actually ran. False means the plan was
    # refused up front (wrong state, or a payload with no `entities` key) and
    # NO extracted name was ever looked at.
    #
    # Callers need this to read `bound`/`review`/`nomatch` correctly: all three
    # are empty both when every name was already bound (a genuine NOOP) and
    # when nothing was examined at all (a refusal). `plan_summary` derives
    # `already_bound` by subtracting those three from the extracted count, so
    # on a refusal it would report every name as already-bound. `reason` cannot
    # stand in for this flag -- the no-ETag branch sets a reason and then
    # carries on resolving.
    examined: bool = False



# Marks a bind that only exists because permissive mode overrode a veto. Written
# into `Decision.reason` by `_promote_top_candidate` -- the ONLY producer -- and
# read back by `is_promoted`. A named constant rather than a literal in two
# places: the console marker and the `.binds.jsonl` audit field must agree, and
# testing a reason string by eye is how they would drift apart.
PROMOTED_PREFIX = "promoted over: "

def is_promoted(decision):
    """True when this bind won by overriding a veto, so it is one to double-check."""
    return decision.reason.startswith(PROMOTED_PREFIX)


def _bind_row(slug, name, decision, notes, section, written):
    """One `*.binds.jsonl` row.

    `reason` is empty for a clean single-candidate match and carries the overridden
    veto for a promoted one, so grepping this file for `promoted over:` lists every
    bind that was a judgement call.
    """
    return {"slug": slug, "extracted": name, "role": section,
            "nes_id": decision.nes_id, "score": decision.score,
            "matched_name": decision.matched_name, "notes": notes,
            "reason": decision.reason, "written": written}


def _promote_top_candidate(decision):
    """A vetoed/ambiguous REVIEW -> a BIND, verdict flipped on the top candidate.

    `resolve` returns REVIEW for ambiguity, truncation, name vetoes,
    cross-script and province/institution scope, and `apply_document_veto` adds
    the election-record one. Each means "a candidate cleared the score threshold
    but something ELSE was unproven", so each has candidates to bind.

    Flips the verdict only. WHICH candidates are bound is `qualifying_binds`'
    decision, and since 2026-08-05 that is all of them, not just this one --
    every veto here is now overridden, including the cross-script match this
    function used to refuse (see the module docstring for the risk that carries).

    Deterministic by construction: `resolve` sorts `candidates` by
    `(-score, nes_id)`, so a re-run binds the same entities in the same order.
    NO_MATCH is left alone -- nothing scored, so there is nothing to promote, and
    creating an entity is the create step's job, not this one's.

    The cost stays explicit: `decision.reason` is carried onto the bind row so
    `*.binds.jsonl` says why the bind was uncertain and lists the candidates.
    """
    if decision.verdict != REVIEW or not decision.candidates:
        return decision
    score, nes_id, matched = decision.candidates[0]
    if score < MIN_BIND_SCORE:
        return decision
    return Decision(BIND, nes_id, score, matched,
                    f"{PROMOTED_PREFIX}{decision.reason}", decision.candidates)


def qualifying_binds(decision):
    """Every candidate on `decision` that may be bound, as its own `Decision`.

    One extracted name can produce SEVERAL binds. `resolve` reports an ambiguity
    as a single REVIEW naming the top candidate; promoting it used to keep that
    one and drop the rest. Since 2026-08-05 every candidate at or above
    `MIN_BIND_SCORE` is bound and the later filtering pass decides -- two NES
    rows scoring identically are usually one entity entered twice, and when they
    are two different people sharing a name, both land and a human unpicks it.

    Only candidates at or above the threshold. "Bind every candidate that
    qualified" is not "bind everything the search returned": a weak near-miss
    riding along on a strong match would bind an unrelated entity.

    Falls back to `[decision]` when there are no candidates to enumerate, which
    is what a hand-built BIND looks like.
    """
    if decision.verdict != BIND:
        return []
    qualifying = [c for c in (decision.candidates or ())
                  if c[0] >= MIN_BIND_SCORE]
    if not qualifying:
        return [decision]
    return [Decision(BIND, nes_id, score, matched,
                     decision.reason, decision.candidates)
            for score, nes_id, matched in qualifying]


def _resolve_with_vetoes(api, name, strict=False, *, section=""):
    """One name -> one `Decision`. THE ONLY resolution path in this module: every
    extracted name and every court-record `accused` name comes through here, so
    no role can drift onto a different guard.

    `strict=False` (the default) binds the best-scoring candidate whenever one
    cleared the threshold, even if a veto fired -- with ONE exception, which the
    fail-closed branch below spells out: an unreadable entity document is never
    promoted. The cross-script refusal that used to be the second exception was
    removed on 2026-08-05, so `कमल थापा` can now bind a `Kamala Thapa` entity.
    `strict=True` restores the conservative behaviour: a veto means REVIEW and a
    human decides.

    Completeness goes IN, so `resolve` applies the truncation veto itself
    alongside the ambiguity check it protects. `search_entities` knows whether it
    ran out of results or stopped early; before this it threw that away and the
    resolver had to guess from a row count. `search_entities` always returns a
    `CandidateList` carrying the real answer. The `True` default covers a caller
    that hands over a plain list -- a stub, or a hand-built set -- which is
    describing exactly the candidates it means, so taking it at its word is right
    rather than cautious-by-reflex. Nothing in production reaches the default.

    A BIND then still needs the document veto: the search payload alone cannot
    tell a real case subject from an Election Commission candidate record sharing
    their name. The second read is wrapped so ANY exception maps to an unreadable
    document, which the veto downgrades to REVIEW. Fail closed -- a transient
    read failure must never let a BIND survive.
    """
    candidates = api.search_entities(name)
    # Only the location section prefers the coded gazetteer entry over an
    # un-coded twin -- see `resolve`. Everywhere else two entities scoring alike
    # is a real ambiguity.
    decision = resolve(name, candidates,
                       candidates_complete=getattr(candidates, "complete", True),
                       prefer_gazetteer=(section == LOCATION_SECTION))
    if not decision.is_bind:
        if strict:
            return decision
        # Promote BEFORE the document read, so a name that `resolve` vetoed still
        # gets its winning candidate checked against its own document below
        # rather than skipping that read entirely.
        decision = _promote_top_candidate(decision)
        if not decision.is_bind:
            return decision

    # What the first promotion overrode, if it happened. `apply_document_veto`
    # REPLACES the reason, so without this the earlier veto is lost: a name that
    # was ambiguous AND looked like an election record would end up recorded as
    # only the second, and `*.binds.jsonl` -- the sole audit trail for permissive
    # mode -- would under-report how uncertain the bind actually was.
    overridden = decision.reason[len(PROMOTED_PREFIX):] if is_promoted(decision) else ""

    read_error = None
    try:
        document = api.get_entity(decision.nes_id)
    except Exception as exc:  # noqa: BLE001 - unreadable == unverified, which is a valid verdict
        document = None  # unreadable == unverified
        read_error = str(exc)
    readable = isinstance(document, dict) and bool(document)
    decision = apply_document_veto(decision, document)
    if overridden and decision.reason != f"{PROMOTED_PREFIX}{overridden}":
        # The veto (or the unreadable-document branch) wrote over the reason.
        decision = Decision(
            decision.verdict, decision.nes_id, decision.score,
            decision.matched_name, f"{decision.reason}; also {overridden}",
            decision.candidates)
    if read_error:
        log.warning("get_entity(%s) failed while veto-checking %r: %s",
                    decision.nes_id if decision.nes_id else "<downgraded>",
                    name, read_error)
        decision = Decision(
            decision.verdict, decision.nes_id, decision.score,
            decision.matched_name,
            f"{decision.reason} (read error: {read_error!r})",
            decision.candidates)
    # An UNREADABLE document stays REVIEW even in permissive mode. Promoting a
    # judgement veto ("this looks like an election-candidate record") is the
    # uncertainty this mode was asked to accept; promoting a failed HTTP read is
    # not -- one 403 or 502 would bind whichever namesake happened to sort first,
    # with nothing having actually been matched against. Distinguished by whether
    # the document came back, never by parsing the veto's reason text.
    if not strict and readable:
        decision = _promote_top_candidate(decision)
    return decision


def bind_section(item):
    """The section one extracted item will actually bind into.

    The coercion in one place because two callers must agree on the answer.
    `plan_case_entities` files the item under this section, and the creation
    stage looks its metadata back up BY that section -- so a caller that read
    the raw `relationship_type` instead would miss every coerced item and
    refuse it for having no prefix.
    """
    rel_type = (item.get("relationship_type") or "").strip().lower()
    return rel_type if rel_type in RELATIONSHIP_TYPES else DEFAULT_RELATIONSHIP_TYPE


def plan_case_entities(api, case, etag, extracted_items, strict=False):
    """Resolve every extracted name for one case and build its write plan.

    Guarantees: never plans a write for a non-DRAFT case, or for a case whose
    payload does not carry an `entities` key at all (see below); only BIND
    decisions that also survive the document veto and the truncation guard
    reach `patch_items`; every item this planner adds is validated before it
    lands there; and the merge emits NOOP when nothing changes, so a re-run is
    idempotent.

    No `required_state` parameter: an earlier version let a caller merge
    against IN_REVIEW, which is exactly what REQUIRED_WRITE_STATE's own
    reasoning above forbids -- IN_REVIEW is publicly retrievable, so a
    non-casework read blanks `notes`, and merging that redacted snapshot into
    the whole-list replace would wipe every existing note. Pinned directly so
    there is no keyword that can reopen that hole.

    `"entities" not in case` is refused rather than merged: `case.get(
    "entities") or []` cannot tell "this case has no binds" from "this payload
    does not carry binds at all" (e.g. a caller passed a trimmed dict). Both
    server read paths do include `entities` today, so this is a defensive
    guard against a payload shape that would otherwise silently plan a
    destructive whole-list replace with every existing bind missing.

    Every extracted name binds into the section its own `relationship_type`
    names, for any of the nine the case API accepts. Three kinds of name do not
    bind: an unrecognised section (no place to file it), a name no NES entity
    matched at all (nothing to file), and an `accused` bind that would escalate an
    entity the case already characterises another way (a human's call, not this
    module's -- see the guard below).

    Bind identity is `(nes_id, relationship_type)`, matching the DB's
    `unique_case_entity_relationship_type` constraint, so one entity may hold two
    binds on a case under different sections. A re-run still produces the same
    pairs and so still writes nothing.

    Scope is a deliberate product decision, not a safety margin, and it was
    widened on request: an earlier version bound `related` only and read
    `accused` solely from the NGM court record, refusing `location` and the rest
    before spending a search. That cost recall -- the resolver bound 33 of 39
    labelled names, the shipped allow-list only 26 -- and the recall is what was
    wanted. `strict=True` restores the old refusals for anyone who needs them.

    What this means in practice, stated plainly because it is the real cost: in
    the default permissive mode a name with several equally-good namesakes binds
    to whichever sorts first, so some binds WILL name the wrong person. The
    run's `*.binds.jsonl` records the reason and the runners-up for every such
    bind, which is the audit trail for finding them again.

    EVERY name here comes from the extraction. `accused` is a section the LLM may
    put a name in like any other; it is no longer also read from the case's NGM
    court record. That path was removed because it needs neither a document nor an
    LLM, so sitting inside this module put it behind five gates it has no use for --
    a case with a complete court record bound zero defendants whenever its
    press-release PDF lacked a MARKDOWN role, or the LLM call failed. See
    `casework/court_record.py`, which is kept and tested but unwired.

    TWO GUARDS SURVIVE PERMISSIVE MODE. Both draw the same line: the caller chose
    to accept uncertainty ABOUT a match, not to invent one.

    1. `apply_document_veto`'s FAIL-CLOSED branch. `api.get_entity` is wrapped in a
       bare try/except, and ANY exception (timeout, 403, 502, a renamed/404 entity)
       or an empty body means the document was never read -- which stays REVIEW.
       Which namesake is right is a judgement the caller chose to accept; an HTTP
       failure is not a judgement at all, and promoting it would bind on evidence
       nobody ever saw. The exception's own text is folded into the reason (and
       logged), so a misconfigured base URL is diagnosable rather than looking like
       a real veto.
    THE CROSS-SCRIPT REFUSAL IS GONE. It used to be the second guard: a
    candidate with no Devanagari name only scored through romanisation, which
    folds a masculine name into its feminine form. Removed on 2026-08-05 when
    the review queue was dropped, so a case charging `कमल थापा` can now bind a
    `Kamala Thapa` entity. Recorded here because this is the function that plans
    the write, and a reader must not infer a guard that no longer exists.
    """
    slug = case.get("slug")
    state = case.get("state")
    plan = EntityBindPlan(slug=slug, action="NOOP", state=state, if_match=etag)
    if state != REQUIRED_WRITE_STATE:
        plan.action = "SKIP_STATE"
        plan.reason = f"state {state!r} != {REQUIRED_WRITE_STATE!r}"
        return plan
    if "entities" not in case:
        plan.reason = (
            "case payload has no 'entities' key -- absent is not empty; "
            "refusing to plan a write from an incomplete read, since merging "
            "would silently drop every existing bind via the whole-list "
            "replace. Re-read the case (get_case_with_etag) before retrying.")
        return plan
    if not etag:
        plan.reason = (
            "no ETag was supplied for this read: the eventual write would go "
            "unconditional (If-Match omitted), so a concurrent edit between "
            "this read and that write would be silently clobbered rather than "
            "rejected with 412. Surfaced here for visibility only -- Task 7's "
            "write path is where this is actually enforced.")

    # Past both refusals: every extracted name below really is looked at, so
    # the three result lists can be read at face value from here on.
    plan.examined = True

    current = current_entity_binds(case)
    plan.n_current = len(current)
    have = {bind_key(bind) for bind in current}
    # No `already_characterised` set here any more. It existed only to feed the
    # accused-escalation guard, and the accused section is refused outright now
    # -- see `_bind_one`. Rebuilding it per case would cost a set build and
    # leave a future reader hunting for the guard it used to serve.

    additions = []
    for item in extracted_items:
        name = (item.get("entity_name") or "").strip()
        if not name:
            continue

        # Bind into whatever section the extraction names. `alleged`, `location`,
        # `witness`, `victim` and the rest all bind.
        #
        # A section the API does not accept is COERCED to `related` rather than
        # held: `related` is what the prompt itself defaults to, and the coercion
        # is not cosmetic. `PATCH /entities` validates the whole list, so one
        # unaccepted section fails every bind on the case rather than only its own
        # row -- holding the name back would cost the other binds nothing, but
        # letting it through would cost them everything.
        raw_type = (item.get("relationship_type") or "").strip().lower()
        rel_type = bind_section(item)
        if rel_type != raw_type:
            plan.coerced.append((name, raw_type, rel_type))

        # THE LLM DOES NOT SUPPLY DEFENDANTS. `GET /courtcases/<court>/<number>/
        # entities` states them exactly -- for 078-CR-0038, हेम राज विष्ट and
        # रुबी जि.सी. विष्ट, the same two the extraction guessed at -- and
        # `casework/court_record.py` already reads it.
        #
        # Dropped rather than coerced to `related`. Coercing would bind a
        # defendant under a section that understates their role, and it would
        # still bind every namesake the search returned.
        #
        # This is also what closes the CHARGED hole: an accused bind carries
        # `outcome = CHARGED`, and since 2026-08-05 one name binds EVERY
        # candidate at or above the threshold, so an ambiguous accused name
        # recorded every namesake as charged -- 13 of them for `संजय प्रसाद
        # यादव`, per `resolve`'s own docstring. With the section gone the path
        # does not exist to narrow.
        if rel_type == ACCUSED_SECTION:
            plan.court_record_only.append((name, rel_type))
            continue

        decision = _resolve_with_vetoes(api, name, strict=strict,
                                        section=rel_type)

        if decision.verdict == REVIEW:
            plan.review.append((name, decision, rel_type))
            continue
        if decision.verdict == NO_MATCH:
            plan.nomatch.append((name, decision, rel_type))
            continue

        notes = (item.get("notes") or "").strip()
        # One name, possibly several binds -- see `qualifying_binds`.
        for bind_decision in qualifying_binds(decision):
            _bind_one(plan, name, bind_decision, rel_type, notes, have,
                      additions)

    merged = merge_entity_binds(current, additions)
    if merged != current:
        plan.action = "WOULD_PATCH"
        plan.patch_items = merged
    return plan


def _bind_one(plan, name, decision, rel_type, notes, have, additions):
    """Add ONE (entity, section) bind to `plan`, or record why it was not added.

    Split out of `plan_case_entities` when one extracted name became able to
    produce several binds -- the body was a `continue`-driven block inside that
    loop, and `continue` cannot mean "next candidate" and "next name" at once.
    Mutates `plan`, `have` and `additions`: the caller's loop owns them, and this
    is the only writer of a bind row.
    """
    item_to_bind = {
        "nes_id": decision.nes_id,
        "relationship_type": rel_type,
        "notes": notes,
    }
    if bind_key(item_to_bind) in have:
        # This entity is already bound to this case IN THIS SECTION (by an
        # earlier extracted name, or by a pre-existing bind) -- not a new
        # addition, so it is not counted in `plan.bound` either. Counting it
        # there would overstate "binds written" on every idempotent re-run.
        #
        # Keyed on the pair, so the same entity CAN still be added in a
        # different section -- see `bind_key`.
        return

    # NO `accused` BRANCH HERE, DELIBERATELY. This used to escalate-guard an
    # accused bind and stamp `outcome = CHARGED`. `plan_case_entities` now
    # refuses the section outright, so both were unreachable -- and code that
    # can stamp CHARGED, sitting in a module that must never write an accused
    # bind, is a hazard waiting for someone to move the filter. The refusal
    # that replaced them is at the write boundary in `validate_bind_item`,
    # where it is reachable and tested.
    # Recorded BEFORE `outcome` is added, so the key matches the one
    # `bind_key` computed above -- it reads only the two identity fields, but
    # adding the entry after the mutation would invite that to drift.
    have.add(bind_key(item_to_bind))
    additions.append(validate_new_bind(item_to_bind))
    plan.bound.append((name, decision, notes, rel_type))


def _check_entity_plan(plan):
    """Raise unless this plan may be written. THE ONLY COPY of the write
    preconditions: `apply_entity_plan` enforces them, `entity_plan_refusal`
    reports them without writing. Two copies would drift, and the direction they
    would drift in is a dry run promising a bind a real run refuses.
    """
    if plan.action != "WOULD_PATCH":
        raise ValueError(
            f"apply_entity_plan called on a {plan.action} plan for {plan.slug!r}")
    if not plan.if_match:
        raise RuntimeError(
            f"refusing unconditional whole-list entities replace for {plan.slug!r}: "
            "no ETag was captured at read time, so a concurrent edit cannot be "
            "detected and the destructive replace could silently clobber it")
    for item in plan.patch_items:
        validate_bind_item(item)


def entity_plan_refusal(plan):
    """Why a real `--apply` run would refuse this plan, or "" if it would write.

    Exists so `would-bind` in a dry run means "would actually bind". The no-ETag
    branch of `plan_case_entities` sets `plan.reason` and then KEEPS RESOLVING by
    design, so such a plan reaches `action == "WOULD_PATCH"` with bound names on
    it. Dry run used to print `WOULD BIND` for those and record `would-bind`,
    while `--apply` hit `_check_entity_plan` and recorded `error` -- overstating,
    in the one output whose entire job is to predict a real run.

    Reports rather than raises, because a dry run is not an error path: the plan
    is refused for this case and the run carries on.
    """
    try:
        _check_entity_plan(plan)
    except (ValueError, RuntimeError) as exc:
        return str(exc)
    return ""


def apply_entity_plan(api, plan):
    """Execute a WOULD_PATCH plan: whole-list replace of /entities, conditional
    on the ETag captured at plan time.

    Uses `replace_list` rather than `patch_field` -- both build the same
    RFC-6902 op, but `replace_list` validates that 'entities' is a whole-list
    path and carries the destructive-replace contract in its docstring.

    Fails closed with no ETag: without If-Match the replace is unconditional and
    a concurrent edit would be silently clobbered.

    NEITHER RETRIES NOR FORCES. A 412 means someone else edited the case between
    the read and this write, so the merged list is built on a stale snapshot and
    writing it would drop their change. The 412 propagates out of
    `api.replace_list`; `main()` catches it, records the case as `error` and emits
    no bind row, so nothing claims a bind that never landed. An operator who wants
    the bind re-runs the enricher, which re-reads the case and rebuilds the merge
    against the current list. Do not add a retry loop here -- a retry that re-uses
    `plan.patch_items` would re-send the same stale list.
    """
    _check_entity_plan(plan)
    return api.replace_list(plan.slug, "entities", plan.patch_items,
                            if_match=plan.if_match)


def source_citation_iri(case):
    """The material IRI to cite on an entity created from this case, or "".

    The first press-release or court-order material with converted text, in that
    order -- the same documents the extraction read, so the citation names a
    document that actually mentions the entity. Empty when neither is present,
    which the caller records rather than papering over: an entity created here
    has no other provenance, and the API's create path enforces none.
    """
    for types in (PRESS_TYPES, COURT_TYPES):
        chunks, _unmet = source_chunks(case, types=types)
        for _mtype, iri, _text in chunks:
            if iri:
                return iri
    return ""


def read_live_prefixes(api):
    """The live prefix list, or None when it could not be read.

    None, not []: an empty list would look like "no prefix is in use" and make
    `prefix_is_creatable` refuse every category. `prefix_is_creatable` itself
    cannot tell the two apart (`set(live_prefixes or ())`), so the distinction
    is only worth anything because `_cannot_create` checks for None BEFORE
    calling it and says what actually happened.

    Every other API call in the per-case loop is wrapped so one case's failure
    does not cost the run. This one was not, and it is called from inside that
    loop -- a single 502 aborted the whole batch, and at the create-step call
    site it did so after entities had already been POSTed.
    """
    try:
        return api.entity_prefixes()
    except Exception as exc:  # noqa: BLE001 - a transient read must cost one case, not the run
        log.warning("could not read the live entity prefixes: %s", exc)
        return None


def create_entities_for_unmatched(api, plan, items_by_key, live_prefixes,
                                  citation, *, dry_run, run_entities):
    """Create an NES entity for each unmatched name, then bind it.

    Returns `(bind_items, still_unmatched, rows)`: the validated bind items to
    merge into the case, the `plan.nomatch` entries no entity could be made for,
    and one report row per name for `*.created.jsonl`.

    `items_by_key` maps `(name, bind_section(item))` back to the extracted item
    the metadata must come from -- the prefix, the type, the English name, the
    notes and the `is_named_entity` gate. KEYED ON THE SECTION TOO, because one
    name can be extracted twice under two sections with contradictory metadata,
    and keyed on the name alone both `plan.nomatch` entries read whichever the
    model emitted last. That is not a tie-break, it is a safety gate decided by
    output order: a contractor extracted as `related` with `is_named_entity:
    true` was refused because a same-named `witness` row said false.

    `run_entities` maps `(prefix, normalised name)` to an IRI already created
    THIS RUN, and is shared across cases on purpose. Case 078-CR-0038 named the
    Dhangadhi forest directorate twice in one extraction; without it, that case
    creates two entities on its first run, and two cases naming the same office
    create two more.

    THE PREFIX IS PART OF THE KEY, and must stay part of it. A person and an
    organisation can carry the same name, and they live at different IRIs
    (`person/ram-bahadur-thapa`, `organization/ram-bahadur-thapa`) that the
    server will never 409 against each other. Keyed on the name alone, the
    second case reuses the first's IRI and binds a PERSON as the organisation
    in a corruption case. The name still carries within a prefix, so two
    spellings of one office -- or one office whose `name_en` the model wrote
    differently in two cases, which would otherwise slug apart -- still collapse
    to one entity.

    A dry run POSTs nothing and still reports the IRI it would have used, built
    from the same prefix and slug, so the printed patch is the one an `--apply`
    run would send.

    Nothing here raises. A name that cannot become an entity -- no prefix, a
    prefix with no existing parent, an unslugifiable name, a failed POST -- is
    recorded and left in `still_unmatched`, so one bad name costs the case
    nothing else.
    """
    bind_items, still_unmatched, rows = [], [], []

    for name, decision, section in plan.nomatch:
        item = items_by_key.get((name, section)) or {}
        prefix = (item.get("entity_prefix") or "").strip().lower()
        etype = (item.get("entity_type") or "").strip()
        name_en = (item.get("name_en") or "").strip()
        row = {"slug": plan.slug, "extracted": name, "role": section,
               "prefix": prefix, "type": etype, "citation": citation,
               "name_en": name_en, "nes_id": "", "outcome": "", "reason": ""}

        slug = entity_slug(name, name_en)
        refusal = _cannot_create(prefix, etype, slug, live_prefixes,
                                 section=section, name=name, item=item)
        if refusal:
            row.update(outcome="skipped", reason=refusal)
            rows.append(row)
            still_unmatched.append((name, decision, section))
            continue

        key = (prefix, normalise_name(name))
        if key in run_entities:
            # Same office, second spelling. Reuse rather than create a twin.
            row.update(outcome="reused", nes_id=run_entities[key])
            rows.append(row)
            bind_items.append(_created_bind(run_entities[key], section, item))
            continue

        iri = build_entity_iri(prefix, slug)
        if dry_run:
            row.update(outcome="would-create", nes_id=iri)
        else:
            try:
                created = api.create_entity(_authoring_payload(
                    prefix, slug, etype, name, citation, name_en))
                iri = created.get("@id") or iri
                row.update(outcome="created", nes_id=iri)
            except EntityAlreadyExists:
                # Someone got there first. That is the outcome we wanted.
                row.update(outcome="already-exists", nes_id=iri)
            except Exception as exc:  # noqa: BLE001 - one name's failure must not cost the case its other binds
                row.update(outcome="error", reason=str(exc))
                rows.append(row)
                still_unmatched.append((name, decision, section))
                continue

        # `_created_bind` VALIDATES, and validation raises. A server answering
        # with an off-authority `@id` -- a redirect, a differently-configured
        # `iri_base()` -- escaped the POST's handler above, which wraps only the
        # request, and the call site has none. That killed the run after
        # entities had already been created. Same treatment as a failed POST:
        # the name is recorded and left unmatched, the case keeps its others.
        try:
            bind = _created_bind(iri, section, item)
        except ValueError as exc:
            row.update(outcome="error", reason=str(exc), nes_id="")
            rows.append(row)
            still_unmatched.append((name, decision, section))
            continue

        run_entities[key] = iri
        rows.append(row)
        bind_items.append(bind)

    return bind_items, still_unmatched, rows


def _cannot_create(prefix, etype, slug, live_prefixes, *, section, name, item):
    """Why this name cannot become an entity, or "" when it can.

    One function so every refusal reads the same way in `created.jsonl`, and so
    the order is fixed: cheapest and most categorical first.

    1. SECTION. NES already holds all 77 districts under official codes
       (`location/district/kailali-np0771`), from a gazetteer ingest. A location
       created here is therefore always a duplicate of a canonical district or
       junk -- there is no third case. Bind them, never mint them.
    2. NAME SHAPE. `_name_vetoes` is the resolver's own judgement that a string
       is too weak to identify anything: a composite `Activity - Location`, a
       lone token, an all-generic institution name. It no longer blocks binding
       (2026-08-05), but a string the resolver will not trust to MATCH is not
       one to CREATE from.
    3. THE MODEL'S VERDICT. `is_named_entity` is the only gate that can tell
       `सामुदायिक वन उपभोक्ता समूह` -- a kind of group -- from a named one.
       `_name_vetoes` cannot: its generic rule needs EVERY word in a 53-word
       list, and neither सामुदायिक nor समूह is in it.

       ABSENT MEANS NO. A prompt regression that drops the field then surfaces
       as `0 created` in the summary, which is visible and fixable; defaulting
       the other way fills NES with entries nobody can delete.
    4. IDENTITY. Prefix, type, slug -- can we even build an IRI. An unreadable
       prefix list is refused here too, but says so in as many words: it is the
       one refusal that reports a failure to check rather than a check that
       failed.
    """
    if section == LOCATION_SECTION:
        return ("location entities are bind-only: NES already holds the "
                "canonical districts under official codes")
    veto = _name_vetoes(name)
    if veto:
        return f"name is not creatable: {veto}"
    if item.get("is_named_entity") is not True:
        return ("extraction did not confirm this is a specific named entity "
                "(is_named_entity)")
    if not prefix or not etype:
        return "extraction gave no entity_prefix/entity_type"
    if live_prefixes is None:
        # NOT a judgement on the prefix -- nothing was checked. `read_live_
        # prefixes` returns None for exactly this case, but `prefix_is_creatable`
        # folds None and [] to the same empty set, so without this branch a
        # transient 502 reports every name as having an unusable prefix. That
        # sentence is false for a prefix as ordinary as `person`, and it sends a
        # caseworker to fix a prefix that was never the problem.
        return (f"the live entity prefix list could not be read, so {prefix!r} "
                "was never checked -- retry this case")
    if not prefix_is_creatable(prefix, live_prefixes):
        return (f"prefix {prefix!r} is not in use and its parent branch does not "
                "exist, so creating it would strand the entity where no search "
                "filter reaches")
    if not slug:
        return "name yields no IRI-legal slug"
    return ""


def _authoring_payload(prefix, slug, etype, name, citation, name_en=""):
    """The API's authoring form for a create POST.

    No `@id`: `normalize_authoring_payload` builds it from prefix+slug and
    validates the shape while doing so (`entities/write_validation.py:113`).

    `name` is a language map keyed `ne`, because every name here comes out of a
    Nepali court document. `en` joins it when the extraction supplied one:
    canonical NES entities carry both (`{"ne": "काठमाडौं", "en": "Kathmandu"}`)
    and every entity this stage created before 2026-08-06 was missing its
    English name, so it was invisible to the English UI and to English search.
    Omitted rather than sent blank -- an empty `en` is a claim that the name has
    no English form, which is different from not knowing it.

    `citation` is a free-form schema.org property the authoring path copies
    through verbatim; it is omitted rather than sent empty when the case had no
    source material to name.
    """
    payload = {
        "prefix": prefix,
        "slug": slug,
        "type": etype,
        "name": {"ne": name},
        "change_description": "Created by casework.enrich_related_entities",
    }
    if name_en:
        payload["name"]["en"] = name_en
    if citation:
        payload["citation"] = citation
    return payload


def _created_bind(iri, section, item):
    """One bind item for a just-created entity, validated like any other."""
    # No accused branch: `validate_new_bind` refuses the section outright, so
    # stamping `outcome = CHARGED` here would only build an item that cannot
    # pass validation.
    bind = {"nes_id": iri,
            "relationship_type": section,
            "notes": (item.get("notes") or "").strip()}
    return validate_new_bind(bind)


def plan_summary(plan, extracted_items):
    """Reconcile one case's plan against the names it was built from.

    `plan_case_entities` (Task 6) drops a resolved BIND whose `nes_id` is
    already bound on the case from `bound`, `review` AND `nomatch` alike --
    correct behaviour for a re-run (there is nothing new to write), but it
    means `len(bound) + len(review) + len(nomatch)` alone silently undercounts
    the extracted names on every re-run: the already-bound ones just vanish.
    Any summary built only from those three counts would be quietly wrong
    every time this enricher is re-run over the same case.

    Recomputed here rather than threaded through `plan_case_entities`, because
    everything needed is already available to a caller that has both the plan
    and the `extracted_items` it was built from. No re-resolution, no extra
    searches -- just arithmetic over what the plan already recorded.

    THE BUCKETS DO NOT SUM TO `extracted`. `bound` counts bind ROWS, and since
    2026-08-05 one extracted name can produce several of them, so
    `bound + review + nomatch` can exceed the number of names. `already_bound`
    is therefore derived from which NAMES produced no row at all, not by
    subtracting row counts -- the subtraction reconciled to -1 on the first
    ambiguity.
    """
    names = [(item.get("entity_name") or "").strip() for item in extracted_items]
    names = [name for name in names if name]
    extracted = len(names)
    bound = len(plan.bound)
    review = len(plan.review)
    nomatch = len(plan.nomatch)
    # `bound` counts bind ROWS and one name can now produce several of them (see
    # `qualifying_binds`), so `extracted - (bound + review + nomatch)` goes
    # NEGATIVE on an ambiguity -- 3 names, 3 binds, 1 no-match reconciled to -1.
    # A name is "accounted for" when it produced at least one row anywhere; the
    # rest are the ones the already-bound check dropped.
    accounted = ({row[0] for row in plan.bound}
                 | {row[0] for row in plan.review}
                 | {row[0] for row in plan.nomatch}
                 # A created name left `nomatch` and produced no row in the other
                 # two either, so it has to be named here or it reads as
                 # already-bound.
                 | set(plan.created))
    already_bound = sum(1 for name in names if name not in accounted)
    return {
        "extracted": extracted,
        "bound": bound,
        "review": review,
        "nomatch": nomatch,
        "created": len(plan.created),
        "already_bound": already_bound,
    }


def report_paths(paths):
    """The three report files, sharing the run log's timestamp-and-run-id stem.

    Guards against blindly slicing off the last 4 characters of any path: a
    log path that genuinely ends in ".log" has that suffix stripped so the
    reports share its stem; a log path that does NOT end in ".log" (a
    different extension, or none) is used as-is rather than having its last 4
    characters silently chopped off -- an unconditional slice would garble
    the stem and scatter the three report files under a name nobody would
    look for. Using the full path as the stem (rather than raising) keeps this
    function tolerant of whatever `configure_run_logging` hands it; the worst
    case is a slightly longer stem (e.g. ".log.binds.jsonl"), never data loss.
    """
    log_path = str(Path(paths["log"]))
    suffix = ".log"
    stem = log_path[: -len(suffix)] if log_path.endswith(suffix) else log_path
    return {"binds": f"{stem}.binds.jsonl",
            "review": f"{stem}.review.jsonl",
            "nomatch": f"{stem}.nomatch.md",
            # `extracted` and `accused_notes` record the model's own answer
            # BEFORE resolution, so a run that binds nothing still shows what it
            # found. Run 645b1483 extracted 13 entities and 2 accused notes and
            # left no trace of either beyond a count in the log.
            "extracted": f"{stem}.extracted.jsonl",
            "accused_notes": f"{stem}.accused_notes.jsonl",
            "created": f"{stem}.created.jsonl"}


#: The no-match report IS the caseworker queue, so an unescaped `|` in an
#: extracted name breaks the row someone is meant to act on. Shared with the
#: review file's own tables.
_md_cell = md_cell


def write_jsonl(path, rows):
    """One JSON object per line, UTF-8, Devanagari unescaped.

    Row-shape agnostic on purpose: `rows` is written exactly as given, one
    `json.dumps` per line, so a caller building a review row that carries the
    full candidate list -- so a reviewer can reproduce a decision from the
    file alone -- is never narrowed to a fixed key set here.
    """
    with open(path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_nomatch_report(path, rows):
    """Unmatched names grouped by normalised form, most-recurring first.

    Ranked by case count because the unmatched names are not one problem each --
    the same district office recurs across cases, so creating a handful of NES
    entities lets a re-run bind many. This file is the caseworker's queue; the
    enricher creates nothing itself.

    Keeps the BEST candidate seen per group: a normalised group can receive
    several `(name, slug, Decision)` rows across different cases, and each
    Decision carries its own `score`/`matched_name` for the closest NES
    candidate that case's search turned up. Tracking only the FIRST row seen
    per group (as opposed to the highest-scoring one) can show a caseworker a
    worse candidate than one this run actually saw for the same name.
    """
    grouped = {}
    for name, slug, decision, section in rows:
        key = normalise_name(name)
        entry = grouped.setdefault(
            key, {"names": [], "slugs": [], "sections": [],
                  "near": decision.matched_name, "score": decision.score})
        if name not in entry["names"]:
            entry["names"].append(name)
        if slug not in entry["slugs"]:
            entry["slugs"].append(slug)
        # EVERY section the group appeared under, not the first. A normalised
        # group collects rows from different cases, and the same name can be
        # extracted as `accused` in one and `location` in another. Showing only
        # one tells a caseworker to create the wrong kind of entity.
        if section and section not in entry["sections"]:
            entry["sections"].append(section)
        if decision.score > entry["score"]:
            entry["near"] = decision.matched_name
            entry["score"] = decision.score
    ordered = sorted(grouped.values(), key=lambda e: (-len(e["slugs"]), e["names"][0]))

    lines = ["# Extracted names with no NES entity", "",
             "Each of these needs an NES entity before a re-run can bind it. "
             "Most-recurring first.", "",
             "| Cases | Extracted name | Role | Closest NES candidate | Score |",
             "|---|---|---|---|---|"]
    for entry in ordered:
        near = _md_cell(entry["near"]) or "—"
        names = " / ".join(_md_cell(name) for name in entry["names"])
        roles = " / ".join(_md_cell(s) for s in entry["sections"]) or "—"
        lines.append(f"| {len(entry['slugs'])} | {names} | {roles} "
                     f"| {near} | {entry['score']:.2f} |")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _truncate_press_release(text, limit=None):
    """Truncate press release, cutting at sentence boundary before limit."""
    if not text:
        return text
    if limit is None:
        limit = PRESS_RELEASE_CHARS
    if len(text) <= limit:
        return text

    chunk = text[:limit]
    for sep in ("।", "\n", ".", "!"):
        idx = chunk.rfind(sep)
        if idx >= limit // 2:
            return chunk[: idx + 1]

    return chunk


def _truncate_court_order(text):
    """Extract the most entity-rich section from a court order."""
    if not text:
        return text

    if len(text) < COURT_ORDER_FULL_THRESHOLD:
        return text

    thahar_marker = "ठहर खण्ड"
    idx = text.find(thahar_marker)
    if idx != -1:
        thahar_text = text[idx:]
        limit = COURT_ORDER_THAHAR_CHARS
        if len(thahar_text) <= limit:
            return f"\n\n[...ठहर खण्ड (verdict section)...]\n\n{thahar_text}"
        chunk = thahar_text[:limit]
        for sep in ("।", "\n", ".", "!"):
            sep_idx = chunk.rfind(sep)
            if sep_idx >= limit // 2:
                chunk = chunk[: sep_idx + 1]
                break
        return f"\n\n[...ठहर खण्ड (verdict section)...]\n\n{chunk}"

    label_head = "\n\n[...court order header section...]\n\n"
    label_tail = "\n\n[...court order verdict section...]\n\n"
    return (
        label_head
        + text[:COURT_ORDER_HEAD_CHARS]
        + label_tail
        + text[-COURT_ORDER_TAIL_CHARS:]
    )


def _enforce_prompt_budget(parts):
    """Ensure combined prompt stays within budget."""
    combined = "\n\n".join(parts)
    if len(combined) <= PROMPT_HARD_MAX:
        return combined

    # Find largest part and truncate
    largest_idx = max(range(len(parts)), key=lambda i: len(parts[i]))
    current_overage = len(combined) - PROMPT_HARD_MAX
    original = parts[largest_idx]
    if len(original) > current_overage + 1000:
        parts[largest_idx] = original[: len(original) - current_overage - 100]

    combined = "\n\n".join(parts)
    return combined[:PROMPT_HARD_MAX]


def _build_content_parts(press_release_text, court_order_text):
    """Build the LLM's user-prompt sections from the two independently-sourced
    texts. Extracted verbatim from the donor's inline `_process_case` (donor
    lines 385-405) into a named, unit-testable function -- the logic itself is
    unchanged: either source alone is sufficient, and the press-release
    truncation limit depends on whether a court order is ALSO present
    (`PRESS_RELEASE_CHARS_NO_COURT` vs `PRESS_RELEASE_CHARS`). One divergence
    from the verbatim donor: an EMPTY court_order_text ("") is now treated the
    same as None (no court present), so a case with fetched-but-empty court text
    still gets the larger no-court press budget instead of being needlessly
    clipped to the with-court limit."""
    content_parts = []

    if press_release_text:
        if not court_order_text:
            truncated = _truncate_press_release(
                press_release_text, limit=PRESS_RELEASE_CHARS_NO_COURT
            )
        else:
            truncated = _truncate_press_release(press_release_text)
        content_parts.append("--- PRESS RELEASE ---")
        content_parts.append(truncated)

    if court_order_text:
        truncated = _truncate_court_order(court_order_text)
        content_parts.append("--- COURT ORDER ---")
        content_parts.append(truncated)

    return content_parts


def _parse_extraction_response(response_text):
    """Extract entities and accused_notes from LLM response JSON."""
    entities = parse_extraction_response(response_text, {"entities"}) or []
    accused_notes = parse_extraction_response(response_text, {"accused_notes"}) or []
    return entities, accused_notes


def build_api(args):
    """Construct the client. Basic (local DEV_AUTH) unless a token is given.

    `allow_remote_writes` is threaded through here for uniformity with the
    other five ported enrichers; unlike them, this module DOES write --
    `apply_entity_plan` calls `api.replace_list` -- so this flag genuinely
    governs whether `--apply` is allowed to reach a non-loopback API base URL.
    """
    if args.api_token:
        return CaseworkApi(
            args.api_base_url, token=args.api_token,
            allow_remote_writes=args.allow_remote_writes,
        )
    return CaseworkApi(
        args.api_base_url,
        basic=basic_auth_from_env(),
        allow_remote_writes=args.allow_remote_writes,
    )


def main(argv=None):
    """Main entry point. Extracts entities via LLM, resolves each to an
    existing NES entity, and binds it -- but only under `--apply`; `--dry-run`
    (the default) prints what WOULD bind without calling `api.replace_list`.
    See the module docstring for the write shape and the guarantees around it.
    """
    ap = argparse.ArgumentParser(
        description=(
            "Extract related and location entities from CIAA cases via LLM, "
            "resolve each to an existing NES entity, and bind it (dry-run by "
            "default; see module docstring)."
        ),
        epilog="Reads cases entirely over the Jawafdehi HTTP API. "
               "Writes to /entities only under --apply.",
    )
    add_common_args(ap)
    ap.add_argument(
        "--create-entities", action="store_true",
        help="Create an NES entity for each extracted name that matches none, "
             "then bind it. OFF BY DEFAULT and never implied by --apply, so "
             "upgrading this enricher cannot make an existing --apply run start "
             "writing to NES. It does not override the dry run either: without "
             "--apply nothing is POSTed and the run only reports what it would "
             "create. Entities created this way are published with NO "
             "sources -- the 2-distinct-publisher rule lives in "
             "`manage.py bulk_ingest`, not on the API's create path.")
    ap.add_argument(
        "--strict", action="store_true",
        help="Bind only when exactly one NES entity matched and no veto fired; "
             "send ambiguities and vetoed matches to review instead. Off by "
             "default: the default binds the best-scoring match for every name, "
             "including a match that exists only across scripts -- that refusal "
             "was removed on 2026-08-05, so a case charging कमल थापा can bind a "
             "Kamala Thapa entity.")
    args = ap.parse_args(argv)

    setup_logging(args.verbose)
    logger, run_id, paths = configure_run_logging("entities", verbose=args.verbose)
    start_time = time.monotonic()

    # Bootstrap Django + LLM (MUST come before importing llm.invoke)
    try:
        bootstrap(args.provider, args.model)
    except Exception as exc:  # noqa: BLE001 - bootstrap failure is reported and exits(1)
        print(f"Bootstrap failed: {exc}", file=sys.stderr)
        sys.exit(1)

    from llm.invoke import invoke_text
    from llm.usage import UsageAccumulator, render_usage_table

    api = build_api(args)
    usage = UsageAccumulator()
    report = RunReport()

    all_cases = list(api.iter_cases())
    cases = select_for_run(all_cases, args)

    total = len(cases)
    log_run_header(
        logger, stage="entities", base_url=args.api_base_url, dry_run=args.dry_run,
        provider=args.provider, model=args.model, n_selected=total,
        run_id=run_id, paths=paths,
    )
    if total == 0:
        print("No matching CIAA case(s) to process.", file=sys.stderr)
        print_summary(report.summary(), args.dry_run, "Related-entity extraction")
        log_run_footer(
            logger, stage="entities", stats=report.summary(),
            duration_s=time.monotonic() - start_time,
        )
        return report

    print(f"Found {total} matching case(s).")
    if args.dry_run:
        print("  --dry-run: printing what WOULD bind; no /entities writes will be made.")
    if args.force:
        print("  --force: re-extracting even for cases with a 'related' bind already present")
    if args.strict:
        print("  --strict: a veto or an ambiguity means REVIEW, not a bind")
    else:
        print("  binding every matched name into the section it was extracted "
              "under; on an ambiguity the best-scoring entity wins, so check "
              "the run's .binds.jsonl for the ones that were uncertain")

    total_entities_extracted = 0
    total_accused_notes_extracted = 0
    total_bound = total_review = total_nomatch = total_already_bound = 0
    # Binds that only exist because permissive mode overrode a veto. Counted
    # separately and printed on its own line: "we bound 40 things" and "9 of
    # those 40 were a judgement call" are different facts, and rolling the
    # second into the first is how the uncertain ones stop getting checked.
    total_promoted = 0
    # Cases skipped by the related-only idempotency gate. Reported separately
    # because since the section widening they are not necessarily finished --
    # see the gate's own comment below.
    total_skipped_enriched = 0
    # Binds that resolved and reached WOULD_PATCH, then lost at the write gate
    # (`entity_plan_refusal` -- a missing ETag, say). They are neither bound nor
    # reviewed nor unmatched, so without their own counter the zero-bind footer
    # below blames the resolver for a refusal that happened after it.
    total_refused_binds = 0
    bind_rows, review_rows, nomatch_rows = [], [], []
    # Collected BEFORE resolution, so they survive a run where nothing binds.
    extracted_rows, accused_notes_rows = [], []
    created_rows = []
    # Entities created THIS RUN, keyed by `(prefix, normalised name)` and shared
    # across cases: the same district office recurs, and each extra creation is a
    # duplicate NES entity nobody asked for. The prefix is in the key so a
    # shared name cannot collapse two categories -- see the call site.
    run_entities = {}
    # Fetched on first use, not at startup -- see the call site.
    live_prefixes = None

    for idx, case in enumerate(cases, 1):
        slug = case.get("slug") or "?"
        title = case.get("title") or ""
        log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                  step="start", status="start", detail=f"[{idx}/{total}] {title[:80]}")

        # A `related` bind is the marker that this stage has already run on this
        # case, and it stays the key. Measured on production: 162 of 3,003 cases
        # carry binds but no `related` one, and a bare `case.get("entities")`
        # test skipped every single one of them.
        #
        # THE KEY IS NOW A PROXY, NOT AN EQUIVALENCE, and this is the deliberate
        # choice. `related` used to be exactly and only what this stage wrote;
        # since the section scope widened it also writes `accused`, `location`,
        # `alleged`, `witness` and the rest. So a case enriched by an earlier
        # related-only run is skipped here with its location and accused names
        # never resolved. Re-keying the gate would re-spend an LLM call on
        # thousands of already-enriched cases, which is a cost decision for
        # whoever runs the campaign -- so the skip is COUNTED and reported in the
        # summary with the `--force` pointer instead of being silently correct-
        # looking. Widened-scope work on an old case is a `--force` re-run.
        #
        # The API is asymmetric: `validate_bind_item` WRITES `relationship_
        # type`, but the read path (`cases/services/nes_resolver.py`, via
        # `CaseSerializer.get_entities`) sends the relationship type back
        # under `type` -- `relationship_type` never appears on a read. Same
        # tolerance `current_entity_binds` already applies just above, so a
        # hand-built dict using either key still behaves correctly.
        existing_related = [
            bind for bind in (case.get("entities") or [])
            if bind_relationship_type(bind) == "related"
        ]
        if existing_related and not args.force:
            total_skipped_enriched += 1
            report.record(
                slug, "entities", "already",
                f"{len(existing_related)} 'related' bind(s) already present")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="idempotency", status="already",
                      detail=f"{len(existing_related)} 'related' bind(s) already present")
            continue

        try:
            detail = api.get_case(slug)
        except Exception as exc:  # noqa: BLE001 - detail-fetch failure falls back to the LIST-shaped case
            detail = case
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="fetch", status="fallback", detail=str(exc),
                      level=logging.WARNING)

        unmet = unmet_prerequisites(STAGE, detail)
        if unmet:
            for reason in unmet:
                report.record(slug, "entities", "unmet", reason)
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="prereq", status="unmet", detail="; ".join(unmet),
                      level=logging.WARNING)
            continue

        press_text, press_unmet = source_text(detail, types=PRESS_TYPES)
        court_text, court_unmet = source_text(detail, types=COURT_TYPES)
        press_text = press_text.strip() or None
        court_text = court_text.strip() or None

        content_parts = _build_content_parts(press_text, court_text)
        if not content_parts:
            # Donor-preserved gate (donor line 404): skip only when BOTH
            # press release and court order content are absent.
            reasons = (press_unmet + court_unmet) or [
                "no press release or court order content"]
            for reason in reasons:
                report.record(slug, "entities", "unmet", reason)
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="source", status="unmet", detail="; ".join(reasons),
                      level=logging.WARNING)
            continue

        if press_text:
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="source", status="ok", detail=f"press release {len(press_text)} chars")
        if court_text:
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="source", status="ok", detail=f"court order {len(court_text)} chars")

        user_prompt = _enforce_prompt_budget(content_parts)
        log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                  step="prompt", status="ok", detail=f"{len(user_prompt)} chars")

        if not user_prompt.strip():
            report.record(slug, "entities", "skipped", "empty prompt after truncation")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="prompt", status="skipped",
                      detail="empty prompt after truncation", level=logging.WARNING)
            continue

        # The category list rides on the system prompt only when we might create
        # something. Fetched once per run, here as well as at the create step,
        # because the prompt is built first.
        if args.create_entities and live_prefixes is None:
            live_prefixes = read_live_prefixes(api)

        try:
            response_text = invoke_text(
                system=SYSTEM_PROMPT + prefix_prompt_section(
                    live_prefixes if args.create_entities else None),
                content=user_prompt,
                max_tokens=EXTRACTION_MAX_TOKENS,
                tier=tier_for("entities"),
                usage=usage,
            )
        except Exception as exc:  # noqa: BLE001 - per-case LLM failure is recorded, run continues
            report.record(slug, "entities", "error", f"LLM extraction failed: {exc}")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="extract", status="error", detail=str(exc),
                      level=logging.ERROR)
            if args.verbose:
                import traceback

                traceback.print_exc()
            continue

        entities_data, accused_notes = _parse_extraction_response(response_text)
        # Only two things are dropped here: a non-dict, and an item with no name.
        # Both are unrecordable -- `plan_case_entities` skips a nameless item
        # without putting it in ANY of its three lists, so `plan_summary` would
        # count it as already-bound (it derives that by subtraction).
        #
        # The relationship_type is deliberately NOT filtered here. It used to be
        # (`in ("location", "related")`), which silently discarded every other
        # section before the planner could see it -- so widening the planner to
        # all nine types would have been dead code for seven of them. One place
        # decides which sections are bindable, and that place is the planner.
        valid_items = [
            item for item in entities_data
            if isinstance(item, dict) and (item.get("entity_name") or "").strip()
        ]

        if not valid_items and not accused_notes:
            report.record(
                slug, "entities", "skipped", "LLM returned no entities or accused notes")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="extract", status="skipped",
                      detail="LLM returned no entities or accused notes",
                      level=logging.WARNING)
            continue

        total_entities_extracted += len(valid_items)
        total_accused_notes_extracted += len(accused_notes)
        log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                  step="extract", status="ok",
                  detail=f"{len(valid_items)} entities + {len(accused_notes)} accused_notes")

        # Record the extraction itself, here, before anything can drop it. Every
        # later exit -- an ETag failure, a refused plan, a whole case of
        # no-matches -- leaves these rows already written.
        for item in valid_items:
            extracted_rows.append({
                "slug": slug,
                "extracted": (item.get("entity_name") or "").strip(),
                "relationship_type": (item.get("relationship_type") or "").strip().lower(),
                "notes": (item.get("notes") or "").strip(),
            })
        for note in accused_notes:
            if isinstance(note, dict):
                accused_notes_rows.append({**note, "slug": slug})

        # Re-read WITH the ETag so the whole-list replace is conditional. `detail`
        # above came from `get_case`, which returns no ETag.
        try:
            fresh, etag = api.get_case_with_etag(slug)
        except Exception as exc:  # noqa: BLE001 - falls back to the stale detail; the case still runs
            fresh, etag = detail, None
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="fetch", status="fallback", detail=str(exc),
                      level=logging.WARNING)

        plan = plan_case_entities(api, fresh, etag, valid_items,
                                  strict=args.strict)

        # Two refusals reach here and NEITHER looked at a single extracted
        # name: a non-DRAFT state, and a payload with no `entities` key. Both
        # must return before `plan_summary` runs -- it derives `already_bound`
        # by subtracting bound/review/nomatch from the extracted count, and on
        # a refusal all three are empty, so every name would be reported as
        # already-bound over two report files that were never touched.
        #
        # Keyed on `plan.examined`, not on `action == "SKIP_STATE"`: the
        # payload refusal leaves `action` at its "NOOP" default and would
        # otherwise fall through into the genuine-NOOP branch below.
        #
        # A wrong state is routine (most cases are not DRAFT); a missing
        # `entities` key means the caller handed over an incomplete read,
        # which is a bug worth surfacing as an error rather than a skip.
        if not plan.examined:
            refused_state = plan.action == "SKIP_STATE"
            report.record(slug, "entities",
                          "skipped" if refused_state else "error", plan.reason)
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="resolve", status="skipped" if refused_state else "error",
                      detail=plan.reason,
                      level=logging.WARNING if refused_state else logging.ERROR)
            continue

        # `role` is the section the extraction ASKED for, which the planner records
        # per review row. It used to be hardcoded `"related"`, then looked up in a
        # name-keyed dict -- both wrong, the second one whenever an extraction
        # names the same person in two sections, where every row got the last
        # section seen. The section is the most useful field on the row for triage,
        # because it says whether an unresolved name was going to be an accused or
        # a district.
        for name, decision, section in plan.review:
            review_rows.append({"slug": slug, "extracted": name,
                                "role": section,
                                "reason": decision.reason, "score": decision.score,
                                "candidates": [list(c) for c in decision.candidates]})
        # CREATE, then bind. Only reachable with --create-entities; without it
        # `plan.nomatch` is untouched and this enricher behaves exactly as before.
        if args.create_entities and plan.nomatch:
            if live_prefixes is None:
                # Fetched once per run, lazily: a run that creates nothing (no
                # unmatched name, or the flag off) must not pay for it.
                live_prefixes = read_live_prefixes(api)
            created_binds, still_unmatched, created = create_entities_for_unmatched(
                api, plan, {((i.get("entity_name") or "").strip(),
                             bind_section(i)): i for i in valid_items},
                live_prefixes, source_citation_iri(detail),
                dry_run=args.dry_run, run_entities=run_entities)
            created_rows.extend(created)
            plan.nomatch = still_unmatched
            # Only outcomes that produced an IRI to bind. `skipped` and `error`
            # stay in `nomatch`, which already accounts for them, and counting
            # them here would report entities we did not create.
            plan.created = [row["extracted"] for row in created
                            if row["outcome"] in CREATED_OUTCOMES]
            for row in created:
                log_event(logger, paths["events"], run_id=run_id, stage="entities",
                          slug=slug, step="create", status=row["outcome"],
                          detail=f"{row['extracted']} -> {row['nes_id'] or row['reason']}",
                          level=logging.WARNING if row["outcome"] in
                          ("skipped", "error") else logging.INFO)
            if created_binds:
                # `patch_items` is already the merged whole list on a WOULD_PATCH
                # plan; on a NOOP it is empty and the case's own binds are the
                # base. Merging against the wrong one would drop every existing
                # bind, because this PATCH replaces the entire list.
                base = plan.patch_items or current_entity_binds(fresh)
                plan.patch_items = merge_entity_binds(base, created_binds)
                plan.action = "WOULD_PATCH"

        for name, decision, section in plan.nomatch:
            nomatch_rows.append((name, slug, decision, section))

        counts = plan_summary(plan, valid_items)
        total_review += counts["review"]
        total_nomatch += counts["nomatch"]
        total_already_bound += counts["already_bound"]

        log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                  step="resolve", status="ok",
                  detail=(f"{len(plan.bound)} bind, {len(plan.review)} review, "
                          f"{len(plan.nomatch)} no-match"))

        if plan.action == "NOOP":
            report.record(slug, "entities", "already",
                          f"{len(plan.review)} for review, {len(plan.nomatch)} no match")
            continue

        # plan.action == "WOULD_PATCH" from here on. Print/record a bind row
        # only once it is genuinely true: in dry-run nothing is ever written,
        # so "WOULD BIND" is accurate immediately; under --apply the write
        # must actually succeed first -- a 412 or a missing ETag must never
        # leave the console or `*.binds.jsonl` claiming a bind that never
        # landed on the server.
        if args.dry_run:
            # A dry run predicts a real run, so it must apply the SAME
            # preconditions `--apply` enforces. Without this, a plan with no
            # captured ETag prints WOULD BIND here and errors under --apply.
            refusal = entity_plan_refusal(plan)
            if refusal:
                total_refused_binds += len(plan.bound)
                report.record(slug, "entities", "would-refuse", refusal)
                log_event(logger, paths["events"], run_id=run_id, stage="entities",
                          slug=slug, step="write", status="would-refuse",
                          detail=refusal, level=logging.WARNING)
                print(f"  WOULD REFUSE {len(plan.bound)} bind(s) on {slug}: {refusal}")
                continue
            total_bound += counts["bound"]
            for name, decision, notes, section in plan.bound:
                bind_rows.append(
                    _bind_row(slug, name, decision, notes, section, False))
                total_promoted += is_promoted(decision)
                print(f"  WOULD BIND ({section}) {name}  ->  {decision.nes_id}  "
                      f"(score {decision.score:.2f})"
                      f"{'  [UNCERTAIN]' if is_promoted(decision) else ''}")
            report.record(slug, "entities", "would-bind",
                          f"{len(plan.bound)} would bind")
            continue

        try:
            apply_entity_plan(api, plan)
        except Exception as exc:  # noqa: BLE001 - a bind failure is recorded per-case and the run continues
            report.record(slug, "entities", "error", f"bind failed: {exc}")
            log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                      step="write", status="error", detail=str(exc), level=logging.ERROR)
            continue

        total_bound += counts["bound"]
        for name, decision, notes, section in plan.bound:
            bind_rows.append(_bind_row(slug, name, decision, notes, section, True))
            total_promoted += is_promoted(decision)
            print(f"  BOUND ({section}) {name}  ->  {decision.nes_id}"
                  f"{'  [UNCERTAIN]' if is_promoted(decision) else ''}")
        report.record(slug, "entities", "bound", f"{len(plan.bound)} bound")
        log_event(logger, paths["events"], run_id=run_id, stage="entities", slug=slug,
                  step="write", status="ok", detail=f"{len(plan.bound)} bound")

    stats = report.summary()
    print_summary(stats, args.dry_run, "Related-entity extraction")
    unmet_reasons = report.unmet_reasons()
    if unmet_reasons:
        print("  unmet reasons:")
        for reason, count in unmet_reasons.most_common():
            print(f"    {count} x {reason}")

    reports = report_paths(paths)
    write_jsonl(reports["binds"], bind_rows)
    write_jsonl(reports["review"], review_rows)
    write_jsonl(reports["extracted"], extracted_rows)
    write_jsonl(reports["accused_notes"], accused_notes_rows)
    write_jsonl(reports["created"], created_rows)
    write_nomatch_report(reports["nomatch"], nomatch_rows)

    print()
    print(f"  TOTAL entities extracted across all cases: {total_entities_extracted}")
    print(f"  TOTAL accused notes extracted: {total_accused_notes_extracted}")
    # "matched an EXISTING entity" and not just "bound": with --create-entities a
    # created entity is bound too, and it is counted on the create line below.
    # Reading 0 here while 13 entities reach the case is the kind of misreport
    # this stage's reporting was rebuilt to stop.
    if args.dry_run:
        # Nothing was written -- say so, so this line can never be mistaken
        # for a record of an actual write the way an unqualified "bound to
        # cases" count could be.
        print(f"  TOTAL that WOULD bind to an EXISTING NES entity (dry run, "
              f"nothing written): {total_bound}")
    else:
        print(f"  TOTAL bound to an EXISTING NES entity: {total_bound}")
    print(f"  TOTAL reported for human review: {total_review}  -> {reports['review']}")
    print(f"  TOTAL with no NES match: {total_nomatch}  -> {reports['nomatch']}")
    if created_rows:
        verb = "WOULD create" if args.dry_run else "created"
        made = sum(1 for row in created_rows if row["outcome"] in CREATED_OUTCOMES)
        print(f"  TOTAL NES entities {verb}: {made}  -> {reports['created']}")
        for outcome in ("skipped", "error"):
            n = sum(1 for row in created_rows if row["outcome"] == outcome)
            if n:
                print(f"    {n} {outcome} (left unmatched)")
    print(f"  TOTAL already bound (nothing to write): {total_already_bound}")
    if total_skipped_enriched:
        # Not necessarily finished, since the section scope widened after those
        # cases were enriched -- see the idempotency gate's comment.
        print(f"  {total_skipped_enriched} case(s) skipped as already enriched, on "
              "the presence of a 'related' bind. A case enriched before the "
              "section scope widened may still have accused/location/witness "
              "names outstanding; re-run those with --force to pick them up.")
    if total_refused_binds:
        print(f"  {total_refused_binds} resolved bind(s) were REFUSED at the write "
              "gate, not rejected by the matcher -- see the WOULD REFUSE lines "
              "above for which precondition failed.")
    if total_promoted:
        print(f"  OF THOSE, {total_promoted} bind(s) overrode a veto -- an "
              "ambiguity between namesakes, a province-scoped office, or an "
              "election-candidate record. Each is marked [UNCERTAIN] above and "
              f"carries a 'promoted over:' reason in {reports['binds']}. These "
              "are the ones to spot-check first.")
    if total_bound == 0:
        if total_entities_extracted == 0:
            # Reachable from three separate skip gates -- the idempotency skip,
            # the prerequisite gate and the no-source gate -- plus an LLM that
            # returns nothing. Without this branch the `else` below fires and
            # claims every extracted name went to review or matched nothing, when
            # no name was extracted at all and both files are empty.
            print("  This run bound zero entities because it extracted none: every "
                  "case was skipped before extraction, or the LLM returned nothing. "
                  "The review and no-match files above are empty. The status counts "
                  "in the summary say which gate each case hit.")
        elif total_refused_binds:
            # Checked before `total_already_bound` and before the generic else:
            # these names DID match and WOULD have bound, so blaming review or a
            # missing NES entity would send a caseworker looking for a resolver
            # problem that is really a write-precondition one.
            print(f"  This run bound zero entities, but {total_refused_binds} bind(s) "
                  "resolved and were refused at the write gate rather than by the "
                  "matcher. Fix the precondition named above and re-run; nothing is "
                  "wrong with those matches.")
        elif total_already_bound:
            print(f"  This run bound zero NEW entities -- {total_already_bound} "
                  "extracted name(s) were already bound on their case(s), nothing "
                  "left to write for them.")
        else:
            print("  This run bound zero entities. Every extracted name either went "
                  "to review or matched no NES entity -- see the two files above.")

    usage_summary = ""
    if usage.calls > 0:
        usage_summary = render_usage_table(
            usage.as_dict()["by_provider"], title="entities usage")
        print()
        print(usage_summary)

    log_run_footer(
        logger, stage="entities", stats=stats,
        duration_s=time.monotonic() - start_time, usage_summary=usage_summary,
    )

    return report


if __name__ == "__main__":
    main()
