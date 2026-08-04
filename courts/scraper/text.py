"""Text normalisers for court-portal HTML, ported from ``ngm.utils.normalizer``.

Pure functions; the portals emit Devanagari digits, mixed date separators, and
liberally padded cells, so every scraped string passes through here before it
reaches a model field.
"""

from __future__ import annotations

import re

from courts.normalize import DEVANAGARI_TO_ASCII

_DEVANAGARI_TO_ASCII_TABLE = str.maketrans(DEVANAGARI_TO_ASCII)


def normalize_whitespace(text: object) -> str:
    """Collapse whitespace runs and strip surrounding quotes/space."""
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) >= 2 and collapsed[0] == collapsed[-1] and collapsed[0] in "\"'":
        collapsed = collapsed[1:-1].strip()
    return collapsed


def nepali_to_roman_numerals(text: object) -> str:
    """Transliterate Devanagari digits (०-९) to ASCII (0-9)."""
    return str(text or "").translate(_DEVANAGARI_TO_ASCII_TABLE)


_ASCII_TO_DEVANAGARI_TABLE = str.maketrans({v: k for k, v in DEVANAGARI_TO_ASCII.items()})


def roman_to_nepali_numerals(text: object) -> str:
    """Transliterate ASCII digits (0-9) to Devanagari (०-९) — some portal search
    forms expect the case number in Devanagari."""
    return str(text or "").translate(_ASCII_TO_DEVANAGARI_TABLE)


def normalize_date(date_str: object) -> str:
    """Normalise a BS date to zero-padded ASCII ``YYYY-MM-DD`` (any separator).

    ``२०८१/०९/२८`` / ``2081.9.28`` / ``2078।05।08`` → ``2081-09-28`` etc. A value
    that isn't a 3-part date is returned normalised-but-unchanged in shape.
    """
    if not date_str:
        return ""
    s = nepali_to_roman_numerals(normalize_whitespace(date_str))
    for sep in ("/", "।", "|", ".", " "):
        s = s.replace(sep, "-")
    parts = s.split("-")
    if len(parts) == 3:
        try:
            return f"{parts[0].zfill(4)}-{parts[1].zfill(2)}-{parts[2].zfill(2)}"
        except (ValueError, IndexError):
            return s
    return s


def fix_parenthesis_spacing(text: object) -> str:
    """``082-CR-0048( पुनरावेदन)`` → ``082-CR-0048 (पुनरावेदन)``."""
    s = normalize_whitespace(text)
    if not s:
        return s
    s = re.sub(r"(\S)\(", r"\1 (", s)  # ensure a space before "("
    s = re.sub(r"\(\s+", "(", s)  # drop space just inside "("
    return re.sub(r"\s+\)", ")", s)  # drop space just before ")"


def coerce_count(value: object) -> int | None:
    """Parse a scraped count (e.g. hearing_count) to an int; ``None`` if unparseable."""
    if value is None:
        return None
    digits = "".join(ch for ch in nepali_to_roman_numerals(value) if ch in "0123456789")
    return int(digits) if digits else None


#: The leading honorific token that begins every judge entry — ``मा.`` (माननीय,
#: abbreviated) or the spelled-out ``माननीय``. Each judge on a multi-judge bench is
#: ``मा. न्या. श्री <name>`` / ``मा. मु. न्या. …`` / ``मा.न्या. …`` / ``माननीय … न्यायाधीश …``;
#: the honorific always opens with one of these, and no judge NAME contains ``मा.``
#: (a bare ``मा`` + period) mid-string, so it is a safe split anchor. The mid-token
#: parts (``न्या.``/``मु.``/``प्र.``) are deliberately NOT anchors — matching them would
#: split inside a single honorific.
_JUDGE_HONORIFIC_RE = re.compile(r"(\S)(मा\.|माननीय)")


def desep_judges(text: object) -> str | None:
    """Re-insert the separator a run-on judges cell lost, joining judges with ``, ``.

    A bench's judges are listed one-per-line (``<br>``) in a single portal cell; a
    bare ``get_text()`` with no separator glues them, so the next judge's honorific
    sticks onto the previous judge's name (``…श्री राममा. न्या. श्री श्याम…``). Every judge
    opens with the माननीय honorific (:data:`_JUDGE_HONORIFIC_RE`), so a comma-space is
    inserted before any honorific glued to a preceding non-space char. Idempotent: an
    already-separated (space/comma/newline-delimited) list is returned unchanged, and
    the FIRST judge (honorific at string start) is never prefixed.
    """
    normalized = normalize_whitespace(text)
    if not normalized:
        return None
    return _JUDGE_HONORIFIC_RE.sub(r"\1, \2", normalized)


def extract_judges(cell) -> str | None:
    """Extract a judges cell as a ``, ``-joined list, honouring ``<br>`` line breaks.

    Replaces ``<br>`` with the separator BEFORE flattening (so structurally-delimited
    benches survive) and runs :func:`desep_judges` as a backstop for cells whose
    judges are split by sibling elements rather than ``<br>``. Returns ``None`` when the
    cell is missing or empty.
    """
    if cell is None:
        return None
    for br in cell.find_all("br"):
        br.replace_with(", ")
    return desep_judges(cell.get_text())
