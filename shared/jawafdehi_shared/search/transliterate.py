"""The single shared Devanagari <-> Latin transliteration implementation.

Owned by ``jawafdehi_shared`` per the unified-search plan (transliteration =
single shared ownership, one impl). All four index-time indexers (entities,
materials, court cases, cases) call ``to_roman`` / ``to_devanagari`` so the
``title_translit`` field is produced one way platform-wide.

Backend: **``indic-transliteration`` (MIT)** — Devanagari <-> Latin via the
``sanscript`` engine. Chosen over Aksharamukha (AGPL-3.0, a copyleft flag for a
hosted service) and IndicXlit (heavy model weights). MIT is safe to vendor.

If the library is not importable in the current environment, both functions fall
back to a documented identity transform (return the input unchanged) and
``backend_available()`` reports ``False`` so callers/tests can detect it. The
fallback is intentionally lossless-for-text but provides NO cross-script bridge;
the in-engine ``icu_transform Any-Latin`` ``.translit`` field (see ``mappings``)
still provides cross-script recall even when the ingest-side romanization is a
no-op. Latin->Devanagari is inherently ambiguous (schema-dependent); treat the
output as a recall booster, never an exact key.
"""

from __future__ import annotations

# Romanization scheme used on the Latin side. IAST is the standard lossless
# Latin-with-diacritics scheme; the index's roman/translit analyzers fold
# diacritics (icu_folding / lowercase), so IAST round-trips through the index as
# plain ASCII for matching while staying reversible here.
_ROMAN_SCHEME = "iast"
_DEVANAGARI_SCHEME = "devanagari"

try:  # pragma: no cover - exercised by backend_available()
    from indic_transliteration import sanscript as _sanscript

    _BACKEND = "indic-transliteration"
except Exception:  # ImportError or any load failure -> documented fallback.
    _sanscript = None
    _BACKEND = None


def backend_available() -> bool:
    """True if the ``indic-transliteration`` backend loaded; False = fallback mode."""
    return _sanscript is not None


def backend_name() -> str:
    """Name of the active backend ("indic-transliteration") or "fallback"."""
    return _BACKEND or "fallback"


def to_roman(text: str) -> str:
    """Transliterate Devanagari ``text`` to a Latin (IAST) form for indexing.

    Non-Devanagari input passes through (sanscript leaves unknown glyphs alone).
    In fallback mode (backend unavailable) returns ``text`` unchanged.
    """
    if not text:
        return text
    if _sanscript is None:
        return text
    return _sanscript.transliterate(text, _DEVANAGARI_SCHEME, _ROMAN_SCHEME)


def to_devanagari(text: str) -> str:
    """Transliterate Latin (IAST/ITRANS-ish) ``text`` to Devanagari for indexing.

    Latin->Devanagari is ambiguous; output is a recall booster, not an exact key.
    In fallback mode returns ``text`` unchanged.
    """
    if not text:
        return text
    if _sanscript is None:
        return text
    return _sanscript.transliterate(text, _ROMAN_SCHEME, _DEVANAGARI_SCHEME)
