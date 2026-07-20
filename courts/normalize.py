"""Case-number normalization for the API read plane.

Ported verbatim from the FastAPI NGM (``ngm.api.normalize``) so the Django
service accepts the same loose case-number forms (Devanagari digits, lowercase,
missing zero-padding) and matches the stored canonical form.
"""

from __future__ import annotations

import re

DEVANAGARI_TO_ASCII = {
    "०": "0",
    "१": "1",
    "२": "2",
    "३": "3",
    "४": "4",
    "५": "5",
    "६": "6",
    "७": "7",
    "८": "8",
    "९": "9",
}

# Middle segment: letter-led alphanumeric, NOT pure letters — district-court
# numbers like 079-C4-3431 make up ~20% of the lake, and a letters-only
# pattern left them un-normalizable, so a lowercase lookup (e.g. from a
# lowercased court-case @id IRI) passed through unchanged and missed the
# stored uppercase row. Letter-led (not [A-Z0-9]+) so all-numeric middles
# (odd legacy ids like 93-068-0194) still pass through verbatim instead of
# being zero-padded away from their stored form.
_CASE_RE = re.compile(r"^(\d+)-([A-Z][A-Z0-9]*)-(\d+)$")


def normalize_case_number(case_number: str) -> str:
    """Normalize a case number to ``XXX-YY-XXXX`` (uppercase, zero-padded).

    Raises ``ValueError`` on a value that can't be parsed into the expected
    digits-letters-digits shape.
    """
    if not case_number:
        raise ValueError("Case number cannot be empty")

    normalized = case_number
    for devanagari, ascii_digit in DEVANAGARI_TO_ASCII.items():
        normalized = normalized.replace(devanagari, ascii_digit)
    normalized = normalized.upper()

    match = _CASE_RE.match(normalized)
    if not match:
        raise ValueError(
            f"Invalid case number format: {case_number}. "
            "Expected format: XXX-YY-XXXX (e.g., 081-CR-0081)"
        )

    first_part, middle_part, last_part = match.groups()
    return f"{first_part.zfill(3)}-{middle_part}-{last_part.zfill(4)}"


def best_effort_normalize(case_number: str) -> str:
    """Normalize if possible, else pass through unchanged.

    A non-conforming value is returned as-is (the lookup just 404s) rather than
    400-ing on legitimately odd ids — mirrors the FastAPI ``_normalize_case_number``.
    """
    try:
        return normalize_case_number(case_number)
    except ValueError:
        return case_number


_SAMET_COUNT_RE = re.compile(r"समेत\s*([0-9]+)")


def parse_stated_defendant_count(defendant_cell: object) -> tuple[int | None, bool]:
    """Parse the court's stated defendant total from an NGM ``defendant`` cell.

    NGM's Special-Court defendant parse is frequently truncated (capped, or only
    the lead defendant), but the court's own summary cell usually states the true
    total as ``"<lead> समेत N"`` ("and N others"), or ends in a bare ``"समेत"``
    when the total is unstated. Returns ``(stated_total, is_bare_samet)``:

    * ``stated_total`` — the ``N`` in ``समेत N`` (Devanagari digits normalized),
      else ``None``.
    * ``is_bare_samet`` — ``True`` when the cell ends in ``समेत`` with no trailing
      number (truncated, magnitude unknown).

    A cell with neither signal returns ``(None, False)`` (no truncation).
    """
    if not defendant_cell:
        return None, False
    text = str(defendant_cell)
    for devanagari, ascii_digit in DEVANAGARI_TO_ASCII.items():
        text = text.replace(devanagari, ascii_digit)
    match = _SAMET_COUNT_RE.search(text)
    if match:
        return int(match.group(1)), False
    # Scraped cells often carry trailing punctuation (Devanagari danda ।/॥,
    # full stop, spaces) after the closing "समेत"; strip it before the check.
    return None, text.rstrip(" \t\r\n।॥.,;:-").endswith("समेत")


def is_verdict_sentinel(value: object) -> bool:
    """True iff ``value`` is the legacy "no verdict date" sentinel.

    The scraper stores ``**** ** **`` (and kin) in ``verdict_date_bs`` when no
    real date exists. Such a value must never reach a CONSUMER as data — the
    importer counts it and the search/material shapers drop it. A value made up
    ONLY of stars / spaces / dashes (after stripping) is a sentinel; an empty or
    None value is treated as absent (also "sentinel" for the drop decision).
    """
    if value is None:
        return True
    stripped = str(value).replace("*", "").replace(" ", "").replace("-", "")
    return stripped == ""


# ── case_type canonicalisation ───────────────────────────────────────────────
# case_type is scraped free text (the मुद्दाको किसिम cause-list / detail cell), so
# clerks paste case numbers, dates and whole sentences into it. The cleaner below
# is deliberately HIGH-PRECISION: it removes only the unambiguous structural noise
# and preserves the label — crucially the statute citations (``चोरी गरेको (दफा 241)``)
# and section references (``१५५ बमोजिम``) that are the MOST useful case_type values
# and merely happen to contain digits. Aggressive rewriting of those would destroy
# real information, so it is intentionally NOT done here.

# A STRUCTURED court case-number token, e.g. "080-C1-0199", "०७९-CP-२३४२". The
# letter segment (C1/CP/CR/WO/OA/FN…) is Latin even when the digits are Devanagari.
_CASE_NUMBER_TOKEN = r"[\d०-९]{2,4}-[A-Za-z][A-Za-z\d०-९]{0,4}(?:-[\d०-९]{1,6})?"
_LEADING_CASE_NUMBER = re.compile(
    r"^\(?\s*(?:" + _CASE_NUMBER_TOKEN + r")\s*\)?[\s,.:;/।\-]*"
)
# Mirror the leading pattern's separator class BEFORE the token, so a token
# preceded by a comma/danda/slash (e.g. "लेनदेन, 080-cp-1852") is consumed whole
# and never leaves a dangling separator. The token itself is still the strict
# NNN-XX-NNNN shape, so statute labels ending in "(दफा 241)" are unaffected.
_TRAILING_CASE_NUMBER = re.compile(
    r"[\s,.:;/।\-]*[(（]?\s*(?:" + _CASE_NUMBER_TOKEN + r")\s*[)）]?\s*$"
)
# भ्रष्टाचार ( X ) — the "corruption ( offense )" wrapper; capture the inner offense.
_BHRASHTACHAR_WRAPPER = re.compile(r"^भ्रष्टाचार\s*\(\s*(.+?)\s*\)\s*$")
# Any Devanagari vowel/consonant (excludes the digit block ०-९ at U+0966–U+096F).
_DEVANAGARI_LETTER = re.compile(r"[ऄ-ह]")
_WHITESPACE = re.compile(r"\s+")


def _has_devanagari_letter(text: str) -> bool:
    return bool(_DEVANAGARI_LETTER.search(text or ""))


def normalize_case_type(case_type: str | None) -> str | None:
    """Canonicalise a scraped ``case_type`` toward its semantic charge/matter label.

    Reversible and safe to run in place over the whole corpus: it unwraps the
    ``भ्रष्टाचार ( X )`` wrapper and strips a leading/trailing STRUCTURED
    case-number token (``NNN-XX-NNNN``), and NOTHING else. Statute citations,
    section references and free-text descriptions are preserved verbatim.

    Matching runs on a whitespace-collapsed, quote-stripped copy (so the tokens
    match regardless of incidental spacing/quotes), but a value is only reported
    as CHANGED when a structural transform actually fires. A value that differs
    from the input by whitespace or surrounding quotes ALONE is returned verbatim
    — so the importer never rewrites (and re-archives / re-counts) it for
    cosmetics. If cleaning would strip the last Devanagari letter (a value that is
    ONLY a case number), the original is returned unchanged, so a value is never
    emptied.
    """
    if not case_type:
        return case_type
    # Whitespace/quote-collapsed working copy — used ONLY for matching, not
    # returned unless a structural transform below actually changes it.
    collapsed = _WHITESPACE.sub(" ", case_type).strip().strip("\"'")
    if not collapsed:
        return case_type

    s = collapsed
    # Apply the strips repeatedly until the value stops changing, so a value
    # carrying MORE THAN ONE kind of noise (e.g. a leading case number AND a
    # भ्रष्टाचार(X) wrapper, where stripping the number first re-exposes the
    # wrapper) is fully normalised in ONE call and the result is a fixed point
    # (idempotent). Each pass can only shrink s, so this always converges; the cap
    # is a belt-and-suspenders guard, not a real bound.
    for _ in range(5):
        before = s
        wrapper = _BHRASHTACHAR_WRAPPER.match(s)
        if wrapper and _has_devanagari_letter(wrapper.group(1)):
            s = wrapper.group(1).strip()

        candidate = _LEADING_CASE_NUMBER.sub("", s, count=1).strip()
        if candidate != s and _has_devanagari_letter(candidate):
            s = candidate

        candidate = _TRAILING_CASE_NUMBER.sub("", s, count=1).strip()
        if candidate != s and _has_devanagari_letter(candidate):
            s = candidate

        if s == before:
            break

    s = s.strip()
    cleaned = s if _has_devanagari_letter(s) else collapsed

    # Persist only a STRUCTURAL change; a whitespace/quote/Unicode-form-only diff
    # returns the raw input so the importer sees no change.
    return cleaned if cleaned != collapsed else case_type
