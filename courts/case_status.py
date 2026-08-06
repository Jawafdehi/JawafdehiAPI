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

# Appellate axis. Everything above describes what a court of FIRST instance did
# with a charge or a claim. An appellate bench instead does something to the
# decision below, and none of the above can say "the judgment under appeal
# stands" — which is the single commonest Supreme outcome (सदर).
AFFIRMED = "AFFIRMED"
REVERSED = "REVERSED"
PARTIALLY_REVERSED = "PARTIALLY_REVERSED"


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
#
# SUBSTRING-matched (``key in decision``), and trial-court vocabulary only — these
# three are the Special Court's charge outcomes. The Supreme Court's appellate
# vocabulary is a different language and lives in _APPELLATE_* below.
#
# **ORDER IS LOAD-BEARING, and used to be wrong.** Matching is first-hit over
# insertion order, so with ``ठहर`` ahead of ``आंशिक`` the cell ``आंशिक ठहर``
# ("partially convicted") matched ``ठहर`` and was recorded as a full CONVICTED.
# Measured on prod before the fix: **593 court_cases carried verdict_type=CONVICTED
# while one of their own hearings said आंशिक …ठहर** (the cells are ``आंशिक ठहर`` ×461
# and ``आदेश >> आंशिक कसुर ठहर सजाय निर्धारणको लागि पेश गर्ने`` ×1,020), plus 5,227 rows
# carry the pattern inside ``extra_data.enrichment_hearings``. Recording a partial
# conviction as a full one is exactly the kind of error this database must not
# make, so the qualifier is now tested FIRST and the general terms follow.
_HEARING_DECISION_MAP = {
    "आंशिक": PARTIALLY_CONVICTED,
    "सफाई": ACQUITTED,
    "ठहर": CONVICTED,
}

# ── Supreme Court appellate vocabulary ───────────────────────────────────────
#
# Derived from the COMPLETE distinct set of ``(status, order_type)`` pairs — 96 of
# them — observed over 20,000 supreme rows with ``verdict_date_ad IS NULL``.
#
# **The `status` label alone cannot be trusted to mean "decided".** The portal
# writes ``अन्तिम आदेश`` ("final order") on plainly interlocutory orders:
# ``कैफियत प्रतिवेदन माग्ने`` (call for a status report) alone accounts for 1,289 of
# the terminal-labelled entries in that sample, and ``प्रत्यर्थी झिकाउने`` (summon the
# respondent) is an opening move, not a disposition. Measured: 1,498 interlocutory
# + 148 referral entries carry a terminal-looking ``status``. Keying off ``status``
# would stamp a verdict date onto thousands of appeals that are still being heard
# — so classification keys off ``order_type``, and an unrecognised value yields
# nothing rather than a guess.
#
# Matching is EXACT on the de-spaced string, not substring: ``रिट खारेज, परमादेश
# जारी`` grants mandamus while dismissing the writ, so a substring hit on ``खारेज``
# would record the opposite of what happened. Compound cells are enumerated
# separately and resolved by the relief actually GRANTED.

#: Trailing UI affordance the portal appends to some cells; no legal content.
_ORDER_UI_SUFFIXES = ("फाइल हेर्नुहोस्",)

#: Zero-width joiner/non-joiner. Invisible, and the corpus is inconsistent about
#: them: ``साधारण तारेखमा राख्‍ने`` and ``साधारण तारेखमा राख्ने`` are the same order
#: differing by one U+200D, so a lookup that keeps them apart misses 83 entries
#: while looking character-identical in every log and diff.
_ZERO_WIDTH = dict.fromkeys((0x200C, 0x200D), None)

#: Orthographic variants the two decades of corpus spell both ways. Applied to the
#: TABLE KEYS and the INPUT alike, so the tables stay written in the modern
#: spelling while still matching the old.
#:
#: ``व``→``ब`` generalises the व/ब unification ``_norm_outcome`` already does for
#: दावी/दाबी — the portals treat the two letters as interchangeable (वदर/बदर,
#: वमोजिम/बमोजिम). It is applied to both sides, so a key containing a legitimate व
#: (वन्दीप्रत्येक्षीकरण) is folded identically and still matches.
_ORDER_SPELLING = (
    ("व", "ब"),
    ("अनुमती", "अनुमति"),   # 16,677 entries hang on this one vowel length
    ("कानुन", "कानून"),
    ("शंशोधन", "संशोधन"),
    ("आंशीक", "आंशिक"),
    ("आशिंक", "आंशिक"),
    ("ईजलाश", "इजलास"),
    ("इजलाश", "इजलास"),
    # Mojibake, not a spelling: some older rows render बमोजिम as बमोजङ्गम (1,330
    # entries). Listed after the व→ब fold, which has already run by this point.
    ("बमोजङ्गम", "बमोजिम"),
    ("सबै‌धानिक", "संबै‌धानिक"),
)

#: order_type → verdict_type, for orders that DISPOSE of the proceeding.
_APPELLATE_OUTCOME_MAP = {
    # the decision below stands / falls
    "सदर": AFFIRMED,
    "आदेश सदर": AFFIRMED,
    "शुरु सदर": AFFIRMED,
    "पुनरावेदनको आदेश सदर": AFFIRMED,
    "आदेश बदर": REVERSED,
    "उल्टी": REVERSED,
    "उल्टि": REVERSED,                      # portal typo for उल्टी
    "उल्टी फैसला": REVERSED,
    "पुनरावेदन उल्टी": REVERSED,
    "आंशिक बदर": PARTIALLY_REVERSED,
    "केही उल्टी": PARTIALLY_REVERSED,
    "केहि उल्टी": PARTIALLY_REVERSED,       # spelling variant of केही
    # writ relief: "जारी" = granted (petitioner won), "खारेज" = refused
    "रिट जारी": CLAIM_UPHELD,
    "परमादेश जारी": CLAIM_UPHELD,
    "उत्प्रेषण जारी": CLAIM_UPHELD,
    "रिट तथा निर्देशनात्मक आदेश जारी": CLAIM_UPHELD,
    "निर्देशनात्मक आदेश जारी": CLAIM_UPHELD,
    "आशिक रिट जारी": PARTIALLY_UPHELD,
    # NB deliberately CLAIM_DENIED, not the _OUTCOME_MAP reading of bare खारेज as
    # QUASHED: when a writ is खारेज it is the PETITION that fails, so nothing is
    # quashed — the status quo survives.
    "रिट खारेज": CLAIM_DENIED,
    "खारेज": CLAIM_DENIED,
    "डिसमिस": DISMISSED,
    # claim/demand outcomes. The portal spells these WITHOUT the space that
    # _OUTCOME_MAP uses ("मागबमोजिम हुने" vs "माग बमोजिम हुने"), which is why the
    # de-spaced lookup below matters: 2,289 entries hang on that one space.
    "मागबमोजिम हुने": CLAIM_UPHELD,
    "मागबमोजिम नहुने": CLAIM_DENIED,
    "मागदाबी पुग्ने": CLAIM_UPHELD,
    "मागदाबी नपुग्ने": CLAIM_DENIED,
    "आंशिक दाबी पुग्ने": PARTIALLY_UPHELD,
    "दाबी नपुग्ने": CLAIM_DENIED,
    "पुनरावेदन जिकिर नपुग्ने": CLAIM_DENIED,
    # ends without a merits ruling
    "मिलापत्र": SETTLED,
    "मुद्दा फिर्ता": WITHDRAWN,
    "निवेदन फिर्ता": WITHDRAWN,
    "पुनरावेदन फिर्ता": WITHDRAWN,
    "तामेली": STRUCK_OFF,
    "रिट तामेली": STRUCK_OFF,
    "लगत कट्टा हुने": STRUCK_OFF,
    "निवेदन खारेज": CLAIM_DENIED,
    "मुद्दा खारेज": CLAIM_DENIED,
    "बदर": REVERSED,
    # the judgment/order under review is altered
    "फैसला संशोधन हुने": AMENDED,
    "आदेश संशोधन हुने": AMENDED,
    "आंशिक संशोधन हुने": AMENDED,
    "निवेदन संशोधन हुने": AMENDED,
    "फैसला संशोधन नहुने": CLAIM_DENIED,
    "आदेश संशोधन नहुने": CLAIM_DENIED,
    "निवेदन संशोधन नहुने": CLAIM_DENIED,
    # leave-to-appeal gateway: refusing leave ends it, granting it is procedural
    "अनुमति हुने": PROCEDURAL,
    "अनुमति नहुने": CLAIM_DENIED,
    "निस्सा हुने": PROCEDURAL,
    "निस्सा नहुने": CLAIM_DENIED,
    "कानून बमोजिम गर्नु": PROCEDURAL,
    "रायबाझी": PROCEDURAL,
    "रायबाझी फैसला": PROCEDURAL,
    "संशोधन हुने": AMENDED,
    "पुनरावेदनको आदेश बदर": REVERSED,
    "माग बमोजिमको आदेश जारी": CLAIM_UPHELD,
    # "affirmed, proceed according to law" — one disposition written several ways,
    # including one badly mangled rendering.
    "सदर (कानून बमोजिम गर्नु)": AFFIRMED,
    "सदर कानून बमोजिम गर्नु": AFFIRMED,
    "सदर ढकानून बमोजिम गर्नुण्": AFFIRMED,
}

#: Compound cells naming two reliefs at once. One verdict_type cannot express
#: both, so each resolves to the relief GRANTED — a petitioner who loses the writ
#: but wins a directive has partly succeeded, not wholly failed.
_APPELLATE_COMPOUND_MAP = {
    "वन्दीप्रत्येक्षीकरण खारेज, परमादेश जारी": PARTIALLY_UPHELD,
    "रिट खारेज, निर्देशनात्मक आदेश जारी": PARTIALLY_UPHELD,
    "रिट खारेज, परमादेश जारी": PARTIALLY_UPHELD,
    "रिट खारेज, अन्य आदेश जारी": PARTIALLY_UPHELD,
    "दाबी नपुग्ने, निर्देशनात्मक आदेश जारी": PARTIALLY_UPHELD,
}

#: Orders that leave the case LIVE. Enumerated rather than inferred so that a new
#: portal value falls through to "unknown" instead of silently counting as decided.
_APPELLATE_INTERLOCUTORY = frozenset({
    "कैफियत प्रतिवेदन माग्ने",
    "कैफियत प्रतिवेदन माग्ने, अल्पकालीन अन्तरकालिन आदेश",
    "लिखित जवाफ माग्ने",
    "प्रत्यर्थी झिकाउने",
    "बयान गराउने",
    "मिसिल झिकाउने",
    "साधारण तारेखमा राख्‍ने",
    "अन्तरिम आदेश जारी",
    "अन्तरिम आदेश जारी, लिखित जवाफ माग्ने",
    "अन्तरिम आदेश जारी नहुने, लिखित जवाफ माग्ने",
    "अवहेलना दर्ता गरी लिखित जवाफ माग्ने",
    "अवहेलना दर्ता गर्ने",
    "धरौटी घटाएको",
    "आदेश बदर धरौटी माग गर्ने",
    "मुल्तवी जगाउने",
    "कानून बमोजिम गर्नु र ध्यानाकर्षण गरिएको",
    # bail/custody rulings and show-cause: the case itself carries on
    "धरौटमा छोड्ने",
    "धरौट कम लिने",
    "कारण देखाउ",
    "साधारण तारेखमा राख्ने",
})

#: Sent to a larger bench, or back down for fresh decision. The case continues
#: elsewhere, so this court has entered no verdict.
_APPELLATE_REFERRAL = frozenset({
    "पूर्ण इजलासमा पेस हुने",
    "पूर्ण ईजलाशमा जाने",
    "पुर्णमा जाने",
    "संवै‌धानिक इजलासमा पेस हुने",
    "संयुक्त इजलासमा पेस हुने",
    "बृहत् पूर्ण इजलासमा पेस हुने",
    "पुनरावेदन जाने",
    "पुनः निर्णयका लागि पठाउने",
    "बदर, सुरु अदालतमा पठाउने",
    "अन्तरिम आदेश जारी नहुने, पूर्ण इजलासमा पेस हुने",
    "अन्तरिम आदेश जारी हुने, पूर्ण इजलासमा पेस हुने",
    "अ।आ। निरन्तरता हुने , पूर्ण इजलास मा पेस गर्ने",
    "जारी",
    # remands and transfers found only in the older corpus
    "बदर, उच्च अदालतमा पठाउने",
    "उल्टी शुरु पठाउने",
    "पूर्ण इजलास जाने",
    "संवै‌धानिक इजलासले हेर्ने",
    "विशेषमा जाने",
})

#: ``status`` values that MIGHT be terminal — a necessary condition, never a
#: sufficient one (see the note above). ``order_type`` decides.
_HEARING_TERMINAL_STATUSES = ("फैसला", "अन्तिम आदेश")

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


@dataclass
class HearingOutcome:
    """A disposition recovered from the enrichment hearing list."""

    verdict_type: str
    #: The deciding sitting's own date, so a caller can fill verdict_date_*.
    verdict_date_bs: str | None = None
    verdict_date_ad: date | None = None
    #: The raw cell, kept so a reviewer can audit the mapping after the fact.
    order_type_raw: str | None = None


def _strip_order_ui(value: object) -> str:
    """Collapse whitespace and drop the portal's trailing UI affordance."""
    text = _ws(value)
    for suffix in _ORDER_UI_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return text


def _order_key(value: object) -> str:
    """The canonical lookup key for an ``order_type`` cell.

    Drops spacing (``मागबमोजिम हुने`` vs ``माग बमोजिम हुने`` — 2,289 entries hang on
    that one space), zero-width joiners, and the orthographic variants above.
    Applied to both the tables and the input so the two always agree.
    """
    text = _despace(_strip_order_ui(value)).translate(_ZERO_WIDTH)
    for variant, canonical in _ORDER_SPELLING:
        text = text.replace(variant, canonical)
    return text


#: Normalised views of the appellate tables, built once through _order_key.
_APPELLATE_OUTCOME_KEYED = {
    _order_key(key): value for key, value in _APPELLATE_OUTCOME_MAP.items()
}
_APPELLATE_COMPOUND_KEYED = {
    _order_key(key): value for key, value in _APPELLATE_COMPOUND_MAP.items()
}
_APPELLATE_INTERLOCUTORY_KEYED = frozenset(
    _order_key(value) for value in _APPELLATE_INTERLOCUTORY
)
_APPELLATE_REFERRAL_KEYED = frozenset(
    _order_key(value) for value in _APPELLATE_REFERRAL
)


def classify_order_type(order_type: object) -> tuple[str, str | None]:
    """``(bucket, verdict_type)`` for one ``order_type`` cell.

    ``bucket`` is one of ``terminal`` / ``compound`` / ``interlocutory`` /
    ``referral`` / ``empty`` / ``unmapped``; ``verdict_type`` is set only for the
    first two. Compound is tried BEFORE the plain table because a compound cell
    contains the plain keys as substrings, and the whole point is not to read
    ``रिट खारेज, परमादेश जारी`` as a bare dismissal.
    """
    key = _order_key(order_type)
    if not key:
        return "empty", None
    if key in _APPELLATE_COMPOUND_KEYED:
        return "compound", _APPELLATE_COMPOUND_KEYED[key]
    if key in _APPELLATE_OUTCOME_KEYED:
        return "terminal", _APPELLATE_OUTCOME_KEYED[key]
    if key in _APPELLATE_INTERLOCUTORY_KEYED:
        return "interlocutory", None
    if key in _APPELLATE_REFERRAL_KEYED:
        return "referral", None
    return "unmapped", None


def outcome_from_hearings(hearings) -> HearingOutcome | None:
    """The case's disposition per its enrichment hearing list, or None.

    ``hearings`` is ``extra_data.enrichment_hearings``. Two key-schemas exist:
    Special uses ``case_status``/``decision_type``, Supreme uses
    ``status``/``order_type`` — either is accepted.

    Walks forward and keeps the LAST disposing sitting, because a case can be
    decided, reopened on review and decided again; the final one is the operative
    result. Interlocutory orders and bench referrals are skipped even when the
    portal labels them ``अन्तिम आदेश`` — that label is not evidence of a verdict.

    Returns None when nothing disposes of the case, which is the common answer
    for a live appeal and must not be confused with "no data".
    """
    if not hearings:
        return None
    found: HearingOutcome | None = None
    for hearing in hearings:
        if not isinstance(hearing, dict):
            continue
        status = _ws(hearing.get("case_status") or hearing.get("status"))
        if status not in _HEARING_TERMINAL_STATUSES:
            continue
        raw = hearing.get("decision_type") or hearing.get("order_type") or ""
        bucket, verdict = classify_order_type(raw)
        if bucket not in ("terminal", "compound"):
            # Fall back to the trial-court substring vocabulary (आंशिक/सफाई/ठहर),
            # which is how Special Court rows have always resolved.
            #
            # Matched against the NORMALISED key, not the raw cell: an older row
            # spelling it आंशीक would otherwise miss a table written आंशिक and be
            # discarded, which is how 62 partial dispositions were being lost.
            decision = _order_key(raw)
            verdict = next(
                (v for k, v in _HEARING_DECISION_MAP.items() if _order_key(k) in decision),
                None,
            )
        # Hoisted out of the fallback branch so it guards BOTH paths with one check.
        # `classify_order_type` only sets a verdict for the terminal/compound buckets
        # (its docstring says so, and both arms return a dict hit), so on the
        # non-fallback path this never fires — but the invariant lives in that
        # function's prose, not its `tuple[str, str | None]` return type. Checking it
        # here is what keeps a verdict-less HearingOutcome unconstructable.
        if verdict is None:
            continue
        date_bs = _ws(hearing.get("date") or hearing.get("hearing_date"))
        date_ad = None
        if date_bs:
            match = _DATE_TOKEN.search(date_bs)
            if match:
                date_bs = _normalize_bs_date(*match.groups())
                date_ad = bs_to_ad(date_bs)
            else:
                date_bs = None
        found = HearingOutcome(
            verdict_type=verdict,
            verdict_date_bs=date_bs or None,
            verdict_date_ad=date_ad,
            order_type_raw=_ws(raw) or None,
        )
    return found


def verdict_from_hearings(hearings) -> str | None:
    """Back-compatible shim: just the ``verdict_type`` of :func:`outcome_from_hearings`.

    Kept because the high/district/supreme/base scrapers all call it by this name
    and want only the enum.
    """
    outcome = outcome_from_hearings(hearings)
    return outcome.verdict_type if outcome else None
