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

import unicodedata

# Romanization scheme used on the Latin side. IAST is the standard lossless
# Latin-with-diacritics scheme; the index's roman/translit analyzers fold
# diacritics (icu_folding / lowercase), so IAST round-trips through the index as
# plain ASCII for matching while staying reversible here.
_ROMAN_SCHEME = "iast"
_DEVANAGARI_SCHEME = "devanagari"

# Colloquial letter substitutions applied to IAST *before* the generic NFKD
# diacritic fold, for the glyphs whose bare-ASCII fold does not match how people
# actually spell the sound in Latin: vocalic ṛ is written "ri" (kṛṣṇa → krishna),
# the sibilants ś/ṣ are "sh" (śarmā → sharma), etc. Everything else (ā ī ū ṭ ḍ ṇ …)
# is handled by the NFKD combining-mark strip in :func:`_fold_diacritics`.
_COLLOQUIAL_MAP = {
    "ṛ": "ri", "ṝ": "ri",
    "ś": "sh", "ṣ": "sh",
    "ñ": "ny", "ṅ": "n",
    "ṃ": "n", "ḥ": "h",
}

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


def _fold_diacritics(text: str) -> str:
    """Strip combining diacritics via NFKD (ā→a, ṇ→n, ī→i, ṭ→t, ḍ→d, …) to ASCII."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in nfkd if not unicodedata.combining(ch))


def _colloquial_fold(iast: str) -> str:
    """IAST → plain-ASCII colloquial spelling: apply the sound-spelling map
    (ś→sh, ṛ→ri, …) then strip the remaining combining diacritics, lowercased."""
    for src, dst in _COLLOQUIAL_MAP.items():
        iast = iast.replace(src, dst)
    return _fold_diacritics(iast).lower()


def _delete_inherent_schwa(iast_token: str) -> str:
    """Delete a single word-final inherent short 'a' (schwa) from an IAST token.

    Runs on IAST (pre-fold) so the long vowel 'ā' — a real vowel, not a schwa — is
    kept: "bharata"→"bharat", "rāma"→"rām", but "sītā" stays "sītā". Tokens of ≤2
    chars (single syllables like "na") are left intact. Only word-final schwa is
    handled; medial-schwa deletion (kāṭhamāḍauṃ→kathmandu) is out of scope for v1.
    """
    if len(iast_token) > 2 and iast_token.endswith("a"):
        return iast_token[:-1]
    return iast_token


def to_roman_colloquial(text: str) -> str:
    """Colloquial, search-friendly romanization of Devanagari ``text``.

    IAST is scholarly ("भरत"→"bharata", with diacritics) and does not match how
    people type Nepali names in Latin ("Bharat"). This folds diacritics to plain
    ASCII and emits BOTH the schwa-kept and word-final-schwa-deleted spellings,
    because word-final schwa deletion is word-dependent — "भरत"→"bharat" (deleted)
    but "कृष्ण"→"krishna" (kept after a cluster). Indexing both forms lets either
    spelling match: "भरत ताल" → "bharata tala bharat tal", "कृष्ण" → "krishna
    krishn". Schwa deletion runs on IAST (before folding) so long 'ā' vowels
    survive. Falls back to ``to_roman`` (identity) when the backend is unavailable.
    """
    roman = to_roman(text)
    if not roman:
        return roman
    # NFC so precomposed 'ā' (U+0101) is one glyph for the schwa endswith("a") check.
    tokens = unicodedata.normalize("NFC", roman).split()
    kept = " ".join(_colloquial_fold(tok) for tok in tokens)
    stripped = " ".join(_colloquial_fold(_delete_inherent_schwa(tok)) for tok in tokens)
    if not stripped or stripped == kept:
        return kept
    return f"{kept} {stripped}"
