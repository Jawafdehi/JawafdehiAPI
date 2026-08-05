"""Accused names, read from the case's own NGM court record.

CURRENTLY UNWIRED -- NOTHING IMPORTS `defendant_names`. This module is complete and
tested; it just has no caller yet. `enrich_related_entities` used to call it, and
that was removed: reading accused needs neither a document nor an LLM, so sitting
inside a document-and-LLM enricher put it behind five gates it has no use for (the
already-enriched skip, the MARKDOWN-role prerequisite, the no-source gate, the
empty-prompt gate, and any LLM failure). A case with a complete court record bound
zero defendants whenever its press-release PDF lacked a MARKDOWN role.

The intended home is its own CLI -- pure HTTP, no model, no token spend, minutes
across the corpus instead of hours. That is pending a decision; do not delete this
in the meantime, and do not wire it back into an LLM enricher.

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
import urllib.error

logger = logging.getLogger(__name__)

# A verdict is legal only on an accused bind (the `outcome_only_on_accused` CHECK
# constraint). Every case in this corpus is a Special Court `-CR-` case, which
# means CIAA filed a charge sheet, so 'charged' is true by construction rather
# than inferred. Sent explicitly so the claim is visible in the request body
# instead of implied by the API's omitted-outcome fallback
# (`cases/api_views.py`).
CHARGED = "charged"

_COURTCASE_MARKER = "/courtcase/"


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
            if (party.get("side") or "").strip().lower() != "defendant":
                continue
            name = (party.get("name") or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            names.append(name)
    return names, skips
