"""Accused names, read from the case's own NGM court record.

WHO CALLS WHAT. `casework.enrich_court_record` is this module's CLI -- pure HTTP,
no model, no token spend -- and it calls `court_record_for_case`, the full read
(detail + hearings + parties), because it needs the dates and the party rows'
`nes_id`/`address`, not just the names. `defendant_names` is the narrow read for
callers that want ONLY the names; it is kept for them and is not on the enricher's
path. Neither is authoritative over the other on WHO COUNTS AS A DEFENDANT: both
route that judgement through `is_defendant`/`party_name` below, so the filter, the
strip and the de-dup cannot drift apart.

Do not wire either of them back into an LLM enricher. `enrich_related_entities`
used to call this module, and that was removed: reading accused needs neither a
document nor an LLM, so sitting inside a document-and-LLM enricher put it behind
five gates it has no use for (the already-enriched skip, the MARKDOWN-role
prerequisite, the no-source gate, the empty-prompt gate, and any LLM failure). A
case with a complete court record bound zero defendants whenever its
press-release PDF lacked a MARKDOWN role.

WHY THIS EXISTS. Accused binds used to have no source at all: the LLM prompt's
PART 3 extracted accused names, counted them, and threw them away. The obvious
fix -- extract accused like everything else -- puts the most consequential bind
on the least reliable input, because an accused bind names a person as the
subject of a corruption case.

The court record is a better source in two ways. It is authoritative (a
defendant is a defendant because a charge sheet says so, not because a model
inferred it), and its names are transcribed rather than summarised.

WHY NAMES AND NOT IDS. NGM's party rows carry an `nes_id` field, and it is
tempting to read the accused bind straight off it. Measured across the 59
published cases, exactly ONE court case has it populated (`special/080-cr-0111`,
where 185 of 186 accused binds match it) -- overall 185 of 659, 28.1%. The other
44 comparable cases store nothing. So the field cannot be the source; resolution
still has to happen, through the same search/resolve/veto path `related` uses.

WHAT THIS MODULE DOES NOT DO. It does not resolve, score, or bind. It answers one
question -- "who does the court record say the defendants are?" -- and leaves the
name-to-entity decision to `casework.entity_resolver`, so there is exactly one
resolution path and one veto path in the enricher.
"""

import logging
import re
import urllib.error

logger = logging.getLogger(__name__)

_COURTCASE_MARKER = "/courtcase/"

#: The one spelling of "defendant" NGM's party rows use (`courts.models`'s
#: `CaseEntity.side` is free text, documented `plaintiff | defendant`).
_DEFENDANT_SIDE = "defendant"

#: Case-type codes whose defendant column names an actual defendant. `""` is
#: the pre-FY073 format (`93-068-0194`), which carries no type segment and is
#: a prosecution -- 139 references in the corpus. Everything else (`OA`, `RE`,
#: the `W*` writ codes, and any code not yet seen) is an allow-list miss and
#: skips: an unrecognised code risks naming a government office as accused,
#: where skipping one only costs a bind a later run recovers.
BINDABLE_CODES = frozenset({"CR", "CB", "FJ", ""})

_CODE_SEGMENT = re.compile(r"-([A-Za-z]+)-")

#: The pre-FY073 shape: three all-ASCII-digit groups, nothing else (`93-068-0194`).
_LEGACY_NUMBER = re.compile(r"^[0-9]+-[0-9]+-[0-9]+$")

#: Returned for a number that is neither `-<letters>-` coded nor the legacy
#: all-digit shape -- NOT in `BINDABLE_CODES`, so it skips and logs rather
#: than joining the `""` (pre-FY073 prosecution) bucket by default. Allow-list,
#: not deny-list: a number this parser cannot read is treated the same as an
#: unrecognised code, never as a silent prosecution.
UNPARSEABLE = "UNPARSEABLE"


def case_number_code(number):
    """The court's case-type letters from `079-CR-0151`, upper-cased.

    `""` only for the genuine pre-FY073 shape (`93-068-0194`); anything else
    this cannot read -- wrong separator, letters in the wrong place, a
    non-ASCII transliteration -- returns `UNPARSEABLE`, never `""`, so it
    cannot be mistaken for that legacy prosecution format.
    """
    text = str(number or "")
    match = _CODE_SEGMENT.search(text)
    if match:
        return match.group(1).upper()
    return "" if _LEGACY_NUMBER.match(text) else UNPARSEABLE


def is_defendant(party):
    """Whether this party row names a defendant rather than a plaintiff.

    The ONE place that test lives. `enrich_court_record._accused_binds` needs
    the whole party row (its `nes_id` and `address`) and so cannot call
    `defendant_names`, but it must not re-spell the filter either: a side test
    that drifts between the two would bind plaintiffs on one path and not the
    other, and `नेपाल सरकार` is the plaintiff on every case in this corpus.
    """
    return (party.get("side") or "").strip().lower() == _DEFENDANT_SIDE


def party_name(party):
    """The party's name, stripped, or "" when the row carries none.

    Shared with `is_defendant` so the two paths cannot disagree on WHICH string
    a party's name is. They no longer de-dup on the same key, though:
    `defendant_names` keys on this exact string, while
    `enrich_court_record._accused_binds` keys on `normalise_name` of it, so that
    two punctuation variants of one name on one case collapse to a single row
    and match the held-name index. Strictly coarser, and benign only because
    `defendant_names` is off the enricher path.
    """
    return (party.get("name") or "").strip()


def court_ref(iri):
    """`(court, case_number)` from a courtcase IRI, or None.

    Refuses anything that is not exactly `<...>/courtcase/<court>/<number>`. A
    malformed reference must not be guessed at: the wrong court/number pair
    would return a different case's defendants, which is the worst possible
    failure for this module.
    """
    text = str(iri or "").strip()
    if _COURTCASE_MARKER not in text:
        return None
    tail = text.split(_COURTCASE_MARKER, 1)[1].strip("/")
    parts = tail.split("/")
    if len(parts) != 2 or not all(part.strip() for part in parts):
        return None
    return parts[0].strip(), parts[1].strip()


def defendant_names(api, case):
    """`(names, skips)` -- the defendants the court record names, in order.

    `names` is de-duplicated across every court reference on the case, order
    preserved. `skips` holds one human-readable line per reference that could
    not be read, so the report can say why a case produced no accused rather
    than silently showing none.

    A reference that 404s is reported, not raised: 9 of the 49 published court
    references do exactly that, and one stale number must not cost the whole
    case its other references.
    """
    refs = [ref for ref in (court_ref(raw) for raw in (case.get("court_cases") or []))
            if ref]
    if not refs:
        return [], ["no court reference on the case: accused cannot be read "
                    "from the court record"]

    names, skips, seen = [], [], set()
    for court, number in refs:
        try:
            parties = api.get_court_case_entities(court, number)
        except urllib.error.HTTPError as exc:
            skips.append(f"court reference {court}/{number} could not be read "
                         f"(HTTP {exc.code})")
            logger.warning("court record %s/%s unreadable: HTTP %s",
                           court, number, exc.code)
            continue
        except Exception as exc:  # noqa: BLE001 - network, decode, anything else: a read failure is a skip
            skips.append(f"court reference {court}/{number} could not be read "
                         f"({type(exc).__name__})")
            logger.warning("court record %s/%s unreadable: %s",
                           court, number, exc)
            continue

        for party in parties:
            if not is_defendant(party):
                continue
            name = party_name(party)
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
    return names, skips


def court_record_for_case(api, case):
    """`(records, skips)` -- the full court record behind every reference on `case`.

    Each record is `{"court", "number", "detail", "hearings", "parties"}`. The
    three reads are made per reference; any one of them failing drops that
    reference into `skips` with a human-readable reason and moves on, because 9
    of the 49 published court references 404 and one stale number must not cost
    a case its other references.

    Deliberately separate from `defendant_names`, which answers the narrower
    "who are the defendants" question and stays the entry point for callers that
    need only names.
    """
    refs = [ref for ref in (court_ref(raw) for raw in (case.get("court_cases") or []))
            if ref]
    if not refs:
        return [], ["no court reference on the case: neither dates nor accused "
                    "can be read from the court record"]

    records, skips = [], []
    for court, number in refs:
        try:
            record = {
                "court": court,
                "number": number,
                "detail": api.get_courtcase(court, number) or {},
                "hearings": api.list_hearings(court, number) or [],
                "parties": api.get_court_case_entities(court, number) or [],
            }
        except urllib.error.HTTPError as exc:
            skips.append(f"court reference {court}/{number} could not be read "
                         f"(HTTP {exc.code})")
            logger.warning("court record %s/%s unreadable: HTTP %s",
                           court, number, exc.code)
            continue
        except Exception as exc:  # noqa: BLE001 - network, decode, anything else: a read failure is a skip
            skips.append(f"court reference {court}/{number} could not be read "
                         f"({type(exc).__name__})")
            logger.warning("court record %s/%s unreadable: %s", court, number, exc)
            continue
        records.append(record)
    return records, skips
