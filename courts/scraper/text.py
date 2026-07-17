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
    s = re.sub(r"(\S)\(", r"\1 (", s)
    s = re.sub(r"\(\s+", "(", s)
    s = re.sub(r"\s+\)", ")", s)
    return s


def coerce_count(value: object) -> int | None:
    """Parse a scraped count (e.g. hearing_count) to an int; ``None`` if unparseable."""
    if value is None:
        return None
    digits = "".join(ch for ch in nepali_to_roman_numerals(value) if ch in "0123456789")
    return int(digits) if digits else None
