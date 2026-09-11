"""Public-search visibility gate for NGM court cases.

The ~1.6M-row court docket is otherwise fully public in unified search
(``/api/search/`` is ``AllowAny`` with no ACL — see ``search/service.py``). This
module decides, per court case, whether it belongs in the PUBLIC index: a curated
corruption / public-accountability slice, NOT a docket mirror. The call sites
(``reindex_courtcases``, ``Importer.reindex``, ``courts.signals``) gate on
``court_case_public_visible(case)`` — a hidden case is simply absent from
``ngm-courtcases``, mirroring the ``materials`` LISTED gate (``reindex_materials``).

Decision (2026-07-21). A court case is SHOWN iff — and a SENSITIVE case is NEVER
shown, overriding everything below:

1. its canonical case-type code is a financial-crime / accountability code
   (``SHOW_CODES``), OR
2. it sits in the corruption FORUM — Special Court or the CIAA ``CR`` series — and
   its code is not purely procedural (``PROCEDURAL_CODES``), OR
3. it is directly referenced by a PUBLISHED Jawafdehi case.

A ``case_type`` absent from the map is *unknown*: rule 1 fails (fail-closed on the
code axis), but forum / publish-link can still surface it.

The sensitive floor has THREE independent inputs, any one of which hides the case:
the canonical code (``SENSITIVE_CODES``), the raw ``case_type`` TEXT
(``names_a_sensitive_offence``), and the court's own anonymisation of a party
(``has_anonymised_party``). The text and party checks exist because the code axis
alone provably failed — see their sections below.

The raw ``case_type`` → canonical code map lives in
``data/case_type_codes.json.gz`` (built offline; REGENERATE it if the importer's
``normalize_case_type`` changes, since the map is keyed on the stored value).
There is no generator in the tree and no review path for its 130k classifications,
so nothing here may depend on the map being right about a protected offence.
"""

from __future__ import annotations

import gzip
import json
import re
import unicodedata
from pathlib import Path
from typing import Any

# ── canonical-code policy — the single source of truth for SHOW/HIDE ──────────
# Financial-crime / public-accountability codes that are public on their own.
SHOW_CODES = frozenset(
    {
        "CORRUPTION",
        "MONEY_LAUNDERING",
        "COOPERATIVE_FRAUD",
        "REVENUE_TAX_CUSTOMS",
        "FRAUD_CHEATING",
    }
)
# Legally/ethically protected personal matters — NEVER shown, even via a forum or
# published-case override (the "sensitive floor").
SENSITIVE_CODES = frozenset(
    {
        "SEXUAL_OFFENSE",
        "DOMESTIC_VIOLENCE",
        "DIVORCE",
        "MARRIAGE",
        "HUMAN_TRAFFICKING",
    }
)
# Generic / procedural buckets (petitions, enforcement, notarial, unspecified) —
# excluded even inside the corruption forum, which is ~76% procedural petitions.
PROCEDURAL_CODES = frozenset(
    {
        "MISC_PETITION",
        "INJUNCTION_INTERIM",
        "EXECUTION_ENFORCEMENT",
        "POWER_OF_ATTORNEY",
        "SCHEDULE_OFFENSE",
        "OTHER_CRIMINAL",
        "UNCATEGORIZED",
    }
)

# ── sensitive floor, axis 2: the raw case_type TEXT ───────────────────────────
# ``case_type`` is free text that frequently names SEVERAL offences at once, and
# the map assigns exactly ONE code per string — in practice the lead offence. So a
# composite charging cheating + kidnapping + rape codes as FRAUD_CHEATING, which is
# in SHOW_CODES: rule 1 fired and the SENSITIVE_CODES floor was never reached,
# because the protected charge had never become the code. Every composite of that
# shape in the corpus was publicly searchable, and the floor above could not have
# caught any of them.
#
# The floor therefore also runs over the raw string: a protected offence named
# ANYWHERE in a composite hides the case, whatever it was coded as. Fail-closed on
# purpose — a wrongly hidden corruption case costs us one search result, a wrongly
# shown one costs a complainant their anonymity.
#
# Matching is spelling-insensitive because the registers are not consistent: the
# same offence appears as जबरजस्ती/जवरजस्ती, मानव/मानब, बेचबिखन/बेचविखन,
# बालविवाह/वालविवाह, and with or without internal spaces — sometimes several
# spellings inside one composite. A literal substring list catches about half.
_DEVANAGARI_ONLY = re.compile(r"[^ऀ-ॿ]+")
# Register-level spelling variation, not linguistics: व/ब and ी/ि are interchanged
# freely by data-entry, ण/न and ष/श less often. Folding them costs precision we do
# not need and buys recall we do.
_SPELLING_FOLD = str.maketrans({"व": "ब", "ी": "ि", "ू": "ु", "ण": "न", "ष": "श"})


def fold_register_text(text: str | None) -> str:
    """Fold a register string to the spelling-insensitive form used for matching.

    NFC-normalises (vowel signs arrive both composed and decomposed), drops ZWNJ /
    ZWJ, strips everything that is not Devanagari — so spacing and punctuation
    stop mattering — then applies ``_SPELLING_FOLD``.
    """
    folded = unicodedata.normalize("NFC", text or "")
    folded = folded.replace("‌", "").replace("‍", "")
    return _DEVANAGARI_ONLY.sub("", folded).translate(_SPELLING_FOLD)


#: Offence names that hide a case outright, written in their ordinary spelling —
#: ``fold_register_text`` handles the variants. Deliberately NOT included:
#:
#: - bare ``बेचबिखन`` ("sale/trade"), which is ordinary language for land and for
#:   drugs — only the ``मानव बेचबिखन`` (human) form is an offence against a person;
#: - bare ``जबरजस्ती`` ("by force"), which qualifies coerced cheque-signing and
#:   forcible lockouts as often as it qualifies करणी.
#:
#: Both would hide legitimate accountability cases for nothing: ``करणी`` alone
#: already carries every rape composite in the corpus.
SENSITIVE_OFFENCE_TERMS = (
    "करणी",  # जबरजस्ती करणी — rape, incl. the …को उद्योग (attempt) forms
    "बलात्कार",
    "यौन",  # यौन दुर्व्यवहार / यौन शोषण / बालयौन
    "अप्राकृतिक मैथुन",
    "मानव बेचबिखन",  # human trafficking
    # The pre-2074 statutory name for trafficking. The map codes the bare form as
    # HOMICIDE (मास्ने read as killing) and its व-spelling as OTHER_CRIMINAL, while
    # coding eight LONGER phrasings of the same offence correctly — so the map is
    # wrong on precisely the shortest and most common form. Those rows are out of
    # the index today only because HOMICIDE happens to miss SHOW_CODES, which is
    # the same accident that already failed once for Supreme 'फौजदारी'.
    "जिउ मास्ने",
    "चेलीबेटी",
    "घरेलु हिंसा",
    "सम्बन्ध विच्छेद",
    "बालविवाह",
    "बहुविवाह",
)
_SENSITIVE_FOLDED = tuple(fold_register_text(t) for t in SENSITIVE_OFFENCE_TERMS)


def names_a_sensitive_offence(case_type: str | None) -> bool:
    """True if the raw ``case_type`` names a protected offence anywhere in it."""
    folded = fold_register_text(case_type)
    return any(term in folded for term in _SENSITIVE_FOLDED)


# ── sensitive floor, axis 3: the court's own anonymisation ────────────────────
# Where a complainant is legally protected, the register replaces their name with a
# pseudonym — "परिवर्तित नाम <locality> <code>". That is the COURT telling us the
# category, directly, and it does not depend on the charge: most such rows are
# recorded as plain ठगी or आपराधिक लाभ, which no case_type rule can catch. We were
# publishing the defendant's name, district, date and offence around an
# already-anonymised victim.
#
# The words are ambiguous and must not be matched bare: the registers use them for
# COMPANY renames too ("X को हाल परिवर्तित नाम Y"), and those sit on revenue and
# corruption cases that are core scope. Excluding parties that carry a company
# marker separated the two cleanly across every marker-bearing case sampled from
# the live index.
#
# Both sides of that comparison are matched on the FOLDED form, exactly like the
# case_type floor: a stray ZWJ, an extra space or a ब/व swap inside the pseudonym
# must not be a way past a privacy gate. Word order varies, so the forms are
# enumerated rather than expressed as a regex over unfolded text.
_ANONYMISED_PARTY_TERMS = (
    "परिवर्तित नाम",  # also covers परिवर्तित नामथर
    "परिवर्तित संकेत नाम",
    "नाम परिवर्तित",
    "नामथर परिवर्तित",
)
_ANONYMISED_PARTY_FOLDED = tuple(fold_register_text(t) for t in _ANONYMISED_PARTY_TERMS)

#: Folded, so they match the same way the pseudonym does. Bare "लि." is NOT here:
#: folded to "लि" it is two characters and would match inside ordinary words.
_COMPANY_MARKERS = (
    "प्रा.लि",
    "प्रा. लि",
    "प्रा.ली",
    "प्रालि",
    "लिमिटेड",
    "कम्पनी",
    "इन्टरप्राइज",
    "उद्योग",
    "इण्डष्ट्रि",
    "इन्डष्ट्रि",
    "इनभेस्टमेन्ट",
    "हायर पर्चेज",
    "सोलुसन",
    "बैंक",
    "फाइनान्स",
    "सहकारी",
    "ट्रेडर्स",
    "सप्लायर्स",
    "मिल्स",
)
_COMPANY_MARKERS_FOLDED = tuple(fold_register_text(m) for m in _COMPANY_MARKERS)

# A party cell describes SEVERAL parties, so the company exclusion has to be judged
# per party. Judged per CELL it becomes a bypass: one renamed company anywhere in
# the cell suppressed the check for an anonymised human named alongside it.
# ``र`` ("and") needs an explicit non-Devanagari boundary — ``\b`` is meaningless
# between Devanagari letters and would split inside ordinary words.
_PARTY_SPLIT_RE = re.compile(r"[,;|/\n]+|(?<![ऀ-ॿ])र(?![ऀ-ॿ])")


def has_anonymised_party(case: Any) -> bool:
    """True if the court anonymised a party — a protected complainant, not a rename.

    Reads the raw ``plaintiff`` / ``defendant`` cells (already loaded by every call
    site's queryset, so this costs no extra query), splits them into individual
    parties, and asks the question of each one separately.

    Where splitting is imperfect it errs toward hiding, which is the correct
    direction for a privacy floor: a company rename wrongly split costs one search
    result, a protected complainant wrongly kept costs them their anonymity.
    """
    for side in ("plaintiff", "defendant"):
        for party in _PARTY_SPLIT_RE.split(getattr(case, side, None) or ""):
            folded = fold_register_text(party)
            if not any(term in folded for term in _ANONYMISED_PARTY_FOLDED):
                continue
            if any(marker in folded for marker in _COMPANY_MARKERS_FOLDED):
                continue
            return True
    return False


_MAP_PATH = Path(__file__).resolve().parent / "data" / "case_type_codes.json.gz"
# CIAA case-number series (NNN-CR-NNNN), e.g. ``081-CR-0081`` (mirrors cases/models).
_CR_RE = re.compile(r"\d{3}-CR-\d{4}")

_code_map: dict[str, str] | None = None
_published_iris: frozenset[str] | None = None


def _load_map() -> dict[str, str]:
    """Load (once) the raw case_type → canonical code map from the gz asset."""
    global _code_map
    if _code_map is None:
        try:
            with gzip.open(_MAP_PATH, "rt", encoding="utf-8") as fh:
                _code_map = json.load(fh)
        except FileNotFoundError:
            _code_map = {}
    return _code_map


def case_type_code(case_type: str | None) -> str | None:
    """Canonical code for a stored ``case_type`` string, or ``None`` if unmapped."""
    if not case_type:
        return None
    return _load_map().get(case_type)


def is_cr_series(case_number: str | None) -> bool:
    """True if the case number is in the CIAA ``CR`` series (``NNN-CR-NNNN``)."""
    return bool(case_number and _CR_RE.search(case_number))


#: Courts where a ``NNN-CR-NNNN`` number means a CIAA prosecution. The CIAA files
#: at the Special Court; every other court's ``CR`` register is its own general
#: criminal docket and has nothing to do with corruption.
CR_SERIES_COURTS = frozenset({"special"})


def in_corruption_forum(case: Any) -> bool:
    """True if the case is in the corruption forum: the Special Court's registers.

    The ``NNN-CR-NNNN`` shape is NOT court-specific — the Supreme Court's general
    criminal register uses it too (7,133 rows: homicide, forgery, cheque dishonour).
    Matching on the number alone therefore swept an entire general criminal docket
    into a public index whose stated scope is "a curated corruption /
    public-accountability slice, NOT a docket mirror".

    That stayed hidden only by accident: Supreme's case_type was the coarse
    'फौजदारी' for every row, which maps to OTHER_CRIMINAL and is excluded as
    procedural. Once those rows were backfilled with their real charges they mapped
    to real codes and passed this rule — an estimated 5,093 of them, ~1,200 homicide.

    Restricting the series rule to the courts where CR means CIAA costs nothing: a
    genuine Supreme corruption appeal has case_type भ्रष्टाचार, which is in
    SHOW_CODES and is shown on the code axis regardless of forum.
    """
    court_id = getattr(case, "court_id", None) or ""
    if court_id == "special":
        return True
    return court_id in CR_SERIES_COURTS and is_cr_series(
        getattr(case, "case_number", None)
    )


def published_referenced_iris(*, refresh: bool = False) -> frozenset[str]:
    """Court-case IRIs directly referenced by a PUBLISHED Jawafdehi case (cached).

    Small set (dozens). Cached process-wide; ``clear_published_cache()`` (called
    from ``cases.signals`` on any Case state change) invalidates it, and the bulk
    reindex commands refresh it at start.
    """
    global _published_iris
    if refresh or _published_iris is None:
        try:
            from cases.models import CaseCourtCaseReference, CaseState

            _published_iris = frozenset(
                CaseCourtCaseReference.objects.filter(
                    case__state=CaseState.PUBLISHED
                ).values_list("courtcase_iri", flat=True)
            )
        except Exception:  # noqa: BLE001 — best-effort: a DB/import hiccup must not break indexing.
            _published_iris = frozenset()
    return _published_iris


def clear_published_cache() -> None:
    """Invalidate the published-reference cache (on Case publish-state change)."""
    global _published_iris
    _published_iris = None


def is_published_referenced(case: Any) -> bool:
    """True if this court case is directly referenced by a PUBLISHED Jawafdehi case."""
    iri = getattr(case, "iri", None)
    return bool(iri) and iri in published_referenced_iris()


def court_case_public_visible(case: Any) -> bool:
    """Whether a court case belongs in the PUBLIC unified-search index.

    See the module docstring for the rule. Pure/read-only except for the cached
    published-reference lookup; degrades to "not referenced" when that query is
    unavailable (e.g. a bare instance in a shaping test).
    """
    if getattr(case, "is_deleted", False):
        return False
    case_type = getattr(case, "case_type", None)
    # The sensitive floor, all three axes, BEFORE any show rule. The code axis is
    # checked last of the three because it is the one that provably missed.
    if names_a_sensitive_offence(case_type):
        return False
    if has_anonymised_party(case):
        return False
    code = case_type_code(case_type)
    if code in SENSITIVE_CODES:
        return False  # sensitive floor — overrides forum + publish-link
    if code in SHOW_CODES:
        return True
    if in_corruption_forum(case) and code not in PROCEDURAL_CODES:
        return True
    return is_published_referenced(case)
