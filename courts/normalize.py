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
