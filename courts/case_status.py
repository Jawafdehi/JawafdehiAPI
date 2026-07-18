"""Parser for the court ``case_status`` free-text field.

The court portals stuff one overloaded string into ``case_status``, so the typed
columns (``verdict_type``, ``verdict_date_*``) were left empty and one Supreme
column-header (``आदेश /फैसलाको किसिम``) leaked in as a status for ~103k rows. This
module turns that raw string into typed fields; the importer's DQ pass writes the
columns from it instead of storing the raw header.

Ported from the NGM scraper (``ngm.utils.case_status_parser``) into the monorepo
as court-case ingestion consolidates here. Three real shapes seen in the corpus:

1. arrow      ``फैसला / अन्तिम आदेश >> <outcome>``   — outcome is enumerable
2. paren-date ``फैसला (मिती: YYYY/MM/DD)``           — date recoverable, outcome not
3. pending    ``चालु`` / ``चलिरहेको`` / ...          — no verdict yet
   invalid    ``आदेश /फैसलाको किसिम``                — a scraped table header, not a status
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from courts.normalize import DEVANAGARI_TO_ASCII
from jawafdehi_shared.dates import bs_to_ad

# --- enums (flat string constants; DB-enum + JSON friendly) ------------------

# lifecycle_status
PENDING = "PENDING"
DECIDED = "DECIDED"
UNKNOWN = "UNKNOWN"

# verdict_type — criminal (charge) outcomes kept distinct from civil (claim) ones
CONVICTED = "CONVICTED"
ACQUITTED = "ACQUITTED"
PARTIALLY_CONVICTED = "PARTIALLY_CONVICTED"
CLAIM_UPHELD = "CLAIM_UPHELD"
CLAIM_DENIED = "CLAIM_DENIED"
PARTIALLY_UPHELD = "PARTIALLY_UPHELD"
SETTLED = "SETTLED"
WITHDRAWN = "WITHDRAWN"
DISMISSED = "DISMISSED"
QUASHED = "QUASHED"
PROCEDURAL = "PROCEDURAL"
ABEYANCE = "ABEYANCE"
STRUCK_OFF = "STRUCK_OFF"
AMENDED = "AMENDED"
OTHER = "OTHER"


# --- vocabulary --------------------------------------------------------------

# Header/label cells that get mis-scraped into case_status (DQ-01). Compared
# space-insensitively (see _despace) because the portals vary the spacing.
_HEADER_LABELS = {
    "आदेश/फैसलाको किसिम",
    "मुद्दाको किसिम",
    "तारेखको किसिम",
    "फैसला/आदेशको किसिम",
}

_PENDING_VALUES = {
    "चालु",
    "चलिरहेको",
    "चली रहेको",
    "विचाराधीन",
}

# Right-hand side of ``… >> <outcome>`` → verdict_type. Keys are pre-normalised by
# _norm_outcome (व→ब unification, whitespace). Long tail falls through to OTHER
# and is flagged ``unmapped`` for the DQ metric.
_OUTCOME_MAP = {
    "माग बमोजिम हुने": CLAIM_UPHELD,
    "दाबी पुग्ने": CLAIM_UPHELD,
    "मिलापत्र": SETTLED,
    "दाबी नपुग्ने": CLAIM_DENIED,
    "अभियोग दाबी पुग्ने": CONVICTED,
    "मुद्दा फिर्ता": WITHDRAWN,
    "डिसमिस": DISMISSED,
    "आंशिक दाबी पुग्ने": PARTIALLY_UPHELD,
    "अधिकृत वारेसनामा प्रमाणित गरिएको": PROCEDURAL,
    "विवाह दर्ता गरिएको": PROCEDURAL,
    "माग बमोजिम नहुने": CLAIM_DENIED,
    "अभियोग दाबी नपुग्ने": ACQUITTED,
    "खारेज": QUASHED,
    "आंशिक अभियोग दाबी पुग्ने": PARTIALLY_CONVICTED,
    "लगत कट्टा गर्ने": STRUCK_OFF,
    "अन्य": OTHER,
    "मुल्तबिमा राख्ने": ABEYANCE,
    "तामेली": STRUCK_OFF,
    "संशोधन हुने": AMENDED,
    "कानून बमोजिम गर्नु": PROCEDURAL,
    "मुल्तवी जगाउने": PROCEDURAL,
}

# Final-hearing ``decision_type`` (from extra_data.enrichment_hearings) → verdict_type.
# Lets Special/Supreme cases resolve an outcome their case_status lacks. Only
# terminal decisions map; interlocutory ones (थुनछेक, साक्षी बुझ्ने, …) stay None.
_HEARING_DECISION_MAP = {
    "सफाई": ACQUITTED,
    "ठहर": CONVICTED,
    "आंशिक": PARTIALLY_CONVICTED,
}

_DECIDED_MARKERS = ("फैसला", "अन्तिम आदेश", "आदेश")

# A BS date token like 2081/09/28, २०८१-०९-२८, 2081।09।28 (any of / - . ।)
_DATE_TOKEN = re.compile(
    r"([०-९0-9]{4})\s*[/\-.।]\s*([०-९0-9]{1,2})\s*[/\-.।]\s*([०-९0-9]{1,2})"
)


@dataclass
class ParsedCaseStatus:
    """Structured view of a raw ``case_status`` string."""

    lifecycle_status: str = UNKNOWN
    verdict_type: str | None = None
    verdict_outcome_raw: str | None = None
    verdict_date_bs: str | None = None
    verdict_date_ad: date | None = None
    # True when the case is DECIDED via the arrow form but the outcome text was
    # not in _OUTCOME_MAP — surfaced as a DQ metric, never silently lost.
    unmapped: bool = False


def _ws(text: object) -> str:
    """Collapse whitespace runs and strip (the portals pad cells liberally)."""
    return " ".join(str(text or "").split())


def _despace(text: str) -> str:
    return text.replace(" ", "")


def _norm_outcome(outcome: str) -> str:
    """Normalise an outcome phrase for map lookup.

    The portals spell the charge word both ``दावी`` and ``दाबी`` (व/ब) — unify to
    ``दाबी`` so both variants hit one key.
    """
    return _ws(outcome).replace("दावी", "दाबी")


#: str.translate table built once from the shared Devanagari→ASCII digit map.
_DEVANAGARI_TO_ASCII_TABLE = str.maketrans(DEVANAGARI_TO_ASCII)


def _normalize_bs_date(year: str, month: str, day: str) -> str:
    """Build a canonical ASCII ``YYYY-MM-DD`` BS date from token groups."""
    y, m, d = (g.translate(_DEVANAGARI_TO_ASCII_TABLE) for g in (year, month, day))
    return f"{y.zfill(4)}-{m.zfill(2)}-{d.zfill(2)}"


def _extract_verdict_date(text: str) -> tuple[str | None, date | None]:
    """Pull a BS verdict date out of ``… (मिती: YYYY/MM/DD)`` etc., if present."""
    m = _DATE_TOKEN.search(text)
    if not m:
        return None, None
    date_bs = _normalize_bs_date(*m.groups())
    return date_bs, bs_to_ad(date_bs)


def parse_case_status(raw: str | None) -> ParsedCaseStatus:
    """Parse a raw ``case_status`` string into typed fields (rules R1–R5)."""
    value = _ws(raw)
    if not value:
        return ParsedCaseStatus(lifecycle_status=UNKNOWN)

    # R1 — header/label artifact leaked in as a status (DQ-01): not a real status.
    if _despace(value) in {_despace(h) for h in _HEADER_LABELS}:
        return ParsedCaseStatus(lifecycle_status=UNKNOWN)

    # R2 — pending / ongoing, in any of its spellings (DQ-05).
    if value in _PENDING_VALUES:
        return ParsedCaseStatus(lifecycle_status=PENDING)

    date_bs, date_ad = _extract_verdict_date(value)

    # R3 — arrow form: outcome is the segment after the last ``>>``.
    if ">>" in value:
        outcome = value.split(">>")[-1].strip()
        verdict = _OUTCOME_MAP.get(_norm_outcome(outcome))
        return ParsedCaseStatus(
            lifecycle_status=DECIDED,
            verdict_type=verdict or OTHER,
            verdict_outcome_raw=outcome or None,
            verdict_date_bs=date_bs,
            verdict_date_ad=date_ad,
            unmapped=verdict is None,
        )

    # R4/R5 — decided marker (with or without a paren date); outcome not in the
    # status itself, so leave verdict_type for the hearing-based resolver.
    if date_bs or value.startswith(_DECIDED_MARKERS):
        return ParsedCaseStatus(
            lifecycle_status=DECIDED,
            verdict_date_bs=date_bs,
            verdict_date_ad=date_ad,
        )

    return ParsedCaseStatus(lifecycle_status=UNKNOWN)


def is_status_artifact(value: str | None) -> bool:
    """True if ``value`` is a scraped table-header/label wrongly stored as a status.

    Used by the DQ pass to clear the ~103k Supreme rows whose case_status is the
    ``आदेश /फैसलाको किसिम`` column header (DQ-01).
    """
    text = _ws(value)
    return bool(text) and _despace(text) in {_despace(h) for h in _HEADER_LABELS}


def verdict_from_hearings(hearings) -> str | None:
    """Best-effort verdict_type from the final decisive hearing.

    ``hearings`` is ``extra_data.enrichment_hearings`` (list of dicts). Used to
    fill an outcome the case_status string never carried (esp. Special/Supreme
    paren-date rows). Returns None when no hearing carries a terminal decision.
    Two hearing key-schemas exist: Special uses ``case_status``/``decision_type``,
    Supreme uses ``status``/``order_type`` — accept either.
    """
    if not hearings:
        return None
    for hearing in reversed(hearings):
        status = (hearing.get("case_status") or hearing.get("status") or "").strip()
        if status != "फैसला":
            continue
        decision = _ws(hearing.get("decision_type") or hearing.get("order_type") or "")
        for key, verdict in _HEARING_DECISION_MAP.items():
            if key in decision:
                return verdict
    return None
