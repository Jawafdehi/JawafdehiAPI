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

_CASE_RE = re.compile(r"^(\d+)-([A-Z]+)-(\d+)$")


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
