"""Bikram Sambat (BS) ↔ Gregorian (AD) date conversion — the single contract.

The Nepali civic corpus dates everything in Bikram Sambat; the search/serving
plane sorts and filters on Gregorian dates. Converting between the two was
previously hand-rolled in three places (the NKP precedent shaper, the
``sync_materials_from_index`` command, and the CIAA draft-case service), each
with a slightly different tolerance for messy input and a different return type.
This module is the one place that conversion lives.

Design:
- :func:`bs_to_ad` is the primitive — it returns a :class:`datetime.date` (or
  ``None``), never raises, and normalizes the messy inputs the corpus actually
  produces (Devanagari digits, ``/`` separators). Callers that need an ISO
  string use :func:`bs_to_ad_iso`.
- The ``nepali`` package (BS calendar tables) is imported lazily so a missing
  dependency degrades to ``None`` rather than an import-time crash — matching the
  most defensive of the prior copies.
"""

from __future__ import annotations

from datetime import date

#: Devanagari digits → ASCII, so a date like ``२०८२-०३-२९`` parses. The corpus
#: mixes Devanagari and ASCII numerals freely; only the CIAA copy handled this,
#: so folding it in here strictly widens what every caller accepts.
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def bs_to_ad(value: object) -> date | None:
    """Convert a Bikram Sambat ``YYYY-MM-DD`` date to a Gregorian :class:`date`.

    Best-effort and total: returns ``None`` for any empty/unparseable/out-of-range
    input and never raises — a date is metadata for sorting, never a hard
    precondition, so a bad one must not tear the caller. Devanagari digits are
    transliterated to ASCII and ``/`` separators are accepted as ``-`` before
    parsing (the forms the scraped corpus actually emits).

    Returns ``None`` if the ``nepali`` package (which provides the BS calendar
    tables) is not installed, rather than failing at import time.
    """
    if not value:
        return None
    try:
        from nepali.datetime import nepalidate
    except ImportError:  # pragma: no cover - nepali is a declared dependency
        return None
    try:
        normalized = str(value).translate(_DEVANAGARI_DIGITS).replace("/", "-")
        parts = normalized.split("-")
        if len(parts) != 3:
            return None
        year, month, day = (int(p) for p in parts)
        return nepalidate(year, month, day).to_datetime().date()
    except Exception:  # noqa: BLE001 — conversion must never hard-fail on bad input.
        return None


def bs_to_ad_iso(value: object) -> str | None:
    """Convert a Bikram Sambat date to a Gregorian ISO date string (``YYYY-MM-DD``).

    Thin wrapper over :func:`bs_to_ad` for the callers that store the AD date as a
    schema.org ``datePublished`` string. ``None`` on any unconvertible input.
    """
    ad = bs_to_ad(value)
    return ad.isoformat() if ad is not None else None


def ad_to_bs(value: object) -> str | None:
    """Convert a Gregorian date to a Bikram Sambat ``YYYY-MM-DD`` string.

    The inverse of :func:`bs_to_ad` — for sources that publish AD dates (e.g. the
    PPMO blacklist API) while the corpus keys on BS. Accepts a
    :class:`datetime.date` or an ISO ``YYYY-MM-DD`` string. Best-effort and total:
    returns ``None`` for empty/unparseable/out-of-range input or a missing
    ``nepali`` package, and never raises.
    """
    if not value:
        return None
    try:
        from nepali.datetime import nepalidate
    except ImportError:  # pragma: no cover - nepali is a declared dependency
        return None
    try:
        if isinstance(value, date):
            ad = value
        else:
            normalized = str(value)[:10].translate(_DEVANAGARI_DIGITS).replace("/", "-")
            parts = normalized.split("-")
            if len(parts) != 3:
                return None
            ad = date(int(parts[0]), int(parts[1]), int(parts[2]))
        bs = nepalidate.from_date(ad)
        return f"{bs.year:04d}-{bs.month:02d}-{bs.day:02d}"
    except Exception:  # noqa: BLE001 — conversion must never hard-fail on bad input.
        return None
