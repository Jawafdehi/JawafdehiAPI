"""Pure natural-key matcher for the jawafdehi case-upload duplicate audit.

A ``/material/jawafdehi/*`` document is one a caseworker attached to a case. Many
are ordinary press releases, court orders, etc. that already exist canonically
elsewhere in the corpus (``ciaa_press_release``, ``court_order``, ...). This module
decides, from a material's JSON-LD ``data`` dict alone, whether it *could* be such
a duplicate and, if so, what canonical material to look for.

It is a PURE function (no DB, no Django) so it unit-tests on sqlite / no DB, and so
the existence check (does that canonical material actually exist?) stays in the
command where the DB lives.

Matching is by NATURAL KEY only — there is no content hash on these documents:

  * ``CIAA_PRESS_RELEASE`` -> the press-release number parsed from the name
    (``…विज्ञप्ति नं. ३१५५…``) -> ``ciaa_press_release/<number>``.
  * ``COURT_ORDER`` / ``COURT_FILING_OTHER`` -> the court-case number
    (``०८१-CR-०१३८``) -> a ``court_order`` whose ident ends in ``.<case-number>``.
  * ``AG_ABHIYOG_PATRA`` / ``LAW_OR_BILL`` / ``OAG_AUDIT_REPORT`` -> the document
    type is known but there is NO shared key to the canonical corpus (the AG
    corpus is keyed by an internal id, not the court-case number), so these are
    reported ``NO_CANONICAL_KEY`` rather than force-matched.
  * ``NEWS`` / ``SOCIAL_MEDIA`` / ``MISC`` (and anything unknown) -> there is no
    canonical source for these at all, so ``NO_CANONICAL_TWIN``.

The numbers live in ``name`` as mixed-script free text (Devanagari digits + ASCII
letters), so parsing normalizes Devanagari digits first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Map Devanagari digits to ASCII so a number embedded in a Nepali name parses.
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

#: The press-release number following ``नं.`` (leading zeros dropped: canonical
#: ``ciaa_press_release`` idents are plain integers, e.g. ``3155``, no padding).
_PR_NUMBER = re.compile(r"नं\.?\s*0*(\d+)")

#: A Nepal court-case number: ``<year>-<1-3 letter code>-<serial>`` (e.g.
#: ``081-CR-0138``, ``075-WF-0005``). The letter code stays ASCII in the name even
#: when the digits are Devanagari, so this runs over the digit-normalized text.
_CASE_NUMBER = re.compile(r"\d{2,4}-[A-Za-z]{1,3}-\d{1,6}")


class Outcome:
    """The matcher's verdict for one jawafdehi material."""

    #: A canonical key was parsed; whether the canonical material EXISTS is the
    #: command's DB check (-> ``duplicate`` vs ``key_but_absent``).
    HAS_KEY = "has_key"
    #: The document type is known, but there is no shared natural key to match on.
    NO_CANONICAL_KEY = "no_canonical_key"
    #: The document type has no canonical source in the corpus at all.
    NO_CANONICAL_TWIN = "no_canonical_twin"


@dataclass(frozen=True)
class CanonicalRef:
    """A candidate canonical material to look for.

    ``ident`` is set for an exact-ident match (press releases). ``case_number`` is
    set for a suffix match on a ``court_order`` ident (``<court>.<case_number>``),
    since the court is not known from the jawafdehi name alone. Exactly one of the
    two is set. ``signal`` is a short human-readable reason for the report.
    """

    source: str
    ident: str | None
    case_number: str | None
    signal: str


def normalize_digits(text: str) -> str:
    """Return ``text`` with Devanagari digits mapped to ASCII (``३१५५`` -> ``3155``)."""
    return (text or "").translate(_DEVANAGARI_DIGITS)


def _text(value: Any) -> str:
    """A display string from a bilingual ``{ne,en}`` dict or a plain string."""
    if isinstance(value, dict):
        return value.get("ne") or value.get("en") or ""
    if isinstance(value, str):
        return value
    return ""


def extract_canonical_key(data: dict) -> tuple[str, CanonicalRef | None]:
    """Classify a jawafdehi material's ``data`` dict.

    Returns ``(outcome, ref)``; ``ref`` is a :class:`CanonicalRef` only when
    ``outcome == Outcome.HAS_KEY``, otherwise ``None``.
    """
    source_type = (data or {}).get("jawafdehi:sourceType")
    name = normalize_digits(_text((data or {}).get("name")))

    if source_type == "CIAA_PRESS_RELEASE":
        m = _PR_NUMBER.search(name)
        if m:
            number = m.group(1)
            return Outcome.HAS_KEY, CanonicalRef(
                source="ciaa_press_release",
                ident=number,
                case_number=None,
                signal=f"CIAA press release no. {number}",
            )
        return Outcome.NO_CANONICAL_KEY, None

    if source_type in ("COURT_ORDER", "COURT_FILING_OTHER"):
        m = _CASE_NUMBER.search(name)
        if m:
            case_number = m.group(0).lower()
            return Outcome.HAS_KEY, CanonicalRef(
                source="court_order",
                ident=None,
                case_number=case_number,
                signal=f"court case {case_number}",
            )
        return Outcome.NO_CANONICAL_KEY, None

    if source_type in ("AG_ABHIYOG_PATRA", "LAW_OR_BILL", "OAG_AUDIT_REPORT"):
        # Type known, but no shared key to the canonical corpus.
        return Outcome.NO_CANONICAL_KEY, None

    # NEWS / SOCIAL_MEDIA / MISC / missing / unknown: no canonical source exists.
    return Outcome.NO_CANONICAL_TWIN, None
