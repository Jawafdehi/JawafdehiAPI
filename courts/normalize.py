"""Normalization for scraped court-case values, on the API read plane.

Three concerns, in the order they were added:

1. **Case numbers** — ported verbatim from the FastAPI NGM (``ngm.api.normalize``)
   so the Django service accepts the same loose forms (Devanagari digits,
   lowercase, missing zero-padding) and matches the stored canonical form.
2. **``case_type``** (``normalize_case_type``) — high-precision structural
   cleanup that deliberately PRESERVES statute citations, because in that field
   the citation is most of the value.
3. **``case_subject``** (``split_case_subject``) — splits the charge from its
   statute citation, because a FACET needs them as separate axes. Opposite
   treatment to (2) on purpose; see the comment above that function.
"""

from __future__ import annotations

import re

from jawafdehi_shared.tags.normalize import normalize_tag

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
    case-number token (``NNN-XX-NNNN``) — dropping an opening paren left orphaned
    when that token sat inside a parenthetical alongside real text — and NOTHING
    else. Statute citations, section references and free-text descriptions are
    preserved verbatim.

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

        # A structured case number can sit INSIDE a parenthetical that also holds
        # real descriptive text, e.g. "हाजिर गराई पाउ ( ज्यान मार्ने उद्योग, 079-C1-0213)".
        # Stripping the trailing ", 079-C1-0213)" removes the CLOSING paren but leaves
        # the opening "(" dangling. Drop that now-orphaned opening paren (keeping the
        # description) so we never emit an unbalanced "(". Guarded on ``s != before``
        # so it fires ONLY as a consequence of a strip this pass — a value's own
        # pre-existing unbalanced parens are never rebalanced when nothing else changed.
        if s != before and (s.count("(") + s.count("（")) > (s.count(")") + s.count("）")):
            idx = max(s.rfind("("), s.rfind("（"))
            if idx != -1 and ")" not in s[idx:] and "）" not in s[idx:]:
                candidate = _WHITESPACE.sub(" ", s[:idx] + " " + s[idx + 1:]).strip()
                if _has_devanagari_letter(candidate):
                    s = candidate

        if s == before:
            break

    s = s.strip()
    cleaned = s if _has_devanagari_letter(s) else collapsed

    # Persist only a STRUCTURAL change; a whitespace/quote/Unicode-form-only diff
    # returns the raw input so the importer sees no change.
    return cleaned if cleaned != collapsed else case_type


# ── case_subject → (charge, statute_section) ─────────────────────────────────
# ``case_subject`` is the other scraped free-text cell, and on the live corpus it
# is the single biggest source of facet noise: the SAME charge appears as
# ``ठगी गरेको`` alone (741) and with three different descriptive parentheticals
# (1126 + 702 + 225), so one charge renders as four filter chips instead of one
# bucket at ~2,794. The statute citation buried in the last group is a BETTER
# filter than the prose it is attached to, so it is lifted into its own field
# rather than discarded.
#
# Unlike ``normalize_case_type`` above — which deliberately PRESERVES statute
# citations because there they are the whole value of the label — this splits them
# out, because a facet needs the charge and the citation as separate axes. Same
# corpus, different field, opposite requirement; both are intentional.
#
# Mechanical only. There is deliberately NO curated charge vocabulary here: folding
# ``कसूर``/``कसुर`` or ``ठगी``/``ठगी गरेको`` together is an editorial judgement with
# no owner yet, and the residue is meant to be measured and handed over, not
# guessed at.

# The statute marker. It sits INSIDE the final group ("(दफा 249 (3)(ग))"), not
# before it, so the group is identified by containing this rather than by position.
_DAFA = "दफा"
_OPEN_PARENS = "(（"
_CLOSE_PARENS = ")）"


def _split_head_and_groups(text: str) -> tuple[str, list[str]]:
    """Split ``text`` into the head before its first TOP-LEVEL paren group, and
    the contents of each top-level group.

    A depth counter, not a regex: the live corpus nests parens inside the
    descriptive group — ``ठगी गरेको (खण्ड (क) वा (ख) मा … गरेमा) (दफा 249 (3)(ग))``
    — so a pattern stripping to the first ``)`` cuts that value in half and leaves
    ``वा (ख) मा … गरेमा)`` behind as prose. An UNTERMINATED group (the corpus
    carries truncated subjects) is closed at end-of-string rather than dropped, so
    a truncated value still yields its charge head instead of nothing.
    """
    depth = 0
    group_start: int | None = None
    first_open: int | None = None
    groups: list[str] = []
    for i, ch in enumerate(text):
        if ch in _OPEN_PARENS:
            if depth == 0:
                group_start = i
                if first_open is None:
                    first_open = i
            depth += 1
        elif ch in _CLOSE_PARENS and depth > 0:
            depth -= 1
            if depth == 0 and group_start is not None:
                groups.append(text[group_start + 1 : i])
                group_start = None
    if depth > 0 and group_start is not None:
        groups.append(text[group_start + 1 :])
    head = text if first_open is None else text[:first_open]
    return head, groups


def _clean_statute(inner: str) -> str | None:
    """``दफा 24९ (३)(ख)`` → ``249(3)(ख)``.

    Digits fold to ASCII and ALL whitespace is removed, because the corpus spells
    the same citation both ways — ``(दफा 24९(३)(ख))`` with mixed-script digits and
    no space, and ``(दफा 249 (3)(ग))`` all-Latin with one — and those must land in
    one bucket. The Devanagari clause letters (क/ख/ग) are NOT digits and stay.
    """
    value = inner.replace(_DAFA, "")
    for devanagari, ascii_digit in DEVANAGARI_TO_ASCII.items():
        value = value.replace(devanagari, ascii_digit)
    value = _WHITESPACE.sub("", value).strip(",;:।-")
    return value or None


def split_case_subject(case_subject: str | None) -> tuple[str | None, str | None]:
    """Split a scraped ``case_subject`` into ``(charge, statute_section)``.

    The charge is the head before the first top-level parenthetical, run through
    the shared tag normalizer (so trailing dandas, doubled spaces and the
    Devanagari ा+े encoding fault are all handled in one place rather than
    re-implemented here). The statute section is lifted out of the last group
    carrying ``दफा``.

    Either half may be ``None``: a subject with no citation has no statute, and a
    subject that is nothing but a parenthetical has no charge. Neither is an error
    and neither is guessed at — an absent value is left absent so the indexer omits
    the field rather than writing a fabricated one.
    """
    if not case_subject or not case_subject.strip():
        return None, None

    collapsed = _WHITESPACE.sub(" ", case_subject).strip()
    head, groups = _split_head_and_groups(collapsed)

    statute: str | None = None
    for inner in reversed(groups):
        if _DAFA in inner:
            statute = _clean_statute(inner)
            break

    charge = normalize_tag(head) or None
    return charge, statute
