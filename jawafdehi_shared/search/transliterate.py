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

import re
import unicodedata
from functools import lru_cache

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
except Exception:  # noqa: BLE001 - ImportError or any load failure -> documented fallback
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


# ── search key ───────────────────────────────────────────────────────────────

#: Sound classes a Nepali speaker typing Latin does NOT distinguish, applied in
#: order. Each is a recall decision with a cost, and each is here because the
#: alternative is a correct guess returning nothing:
#:
#: * ``ksh``/``chh`` — क्ष is read "chha" in Nepali, so क्षेत्र is typed both
#:   "kshetra" and "chhetra".
#: * ``sh``->``s`` — श, ष and स all collapse in casual spelling (sharma/sarma).
#: * ``ee``/``oo`` and doubled vowels — a Latin keyboard does not carry
#:   Devanagari vowel length.
#:
#: Over-matching is the intended failure mode: on a listing bounded by one
#: document this widens the result by a few rows, while under-matching hides the
#: row the reader came for.
_SEARCH_SUBSTITUTIONS: tuple[tuple[str, str], ...] = (
    ("ksh", "ch"),
    ("chh", "ch"),
    ("sh", "s"),
    ("ee", "i"),
    ("oo", "u"),
    ("ng", "n"),
)

#: Almost nobody types the ``h`` of an aspirate: बढी is written "badi" as often
#: as "badhi".
_ASPIRATE_RE = re.compile(r"([bcdgjkpt])h")

#: व is romanized "v", spoken closer to "w", and written "b" as often as either
#: (विवरण/बिबरण, विवेक/बिबेक). Collapsing all three is the single biggest recall
#: win on this corpus, whose most common word is विवरण.
_GLIDE_RE = re.compile(r"[vw]")

_REPEAT_RE = re.compile(r"([a-z0-9])\1+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _clean_for_search(text: str) -> str:
    """NFC, minus the invisible characters OCR leaves in Devanagari conjuncts.

    Mirrors the ``nfc_normalizer`` + ``strip_zerowidth`` char filters the index
    analyzers already run (see ``mappings``), and for the same reason: this
    corpus is OCR output, and a stray U+200C inside क्‌षेत्र makes the
    transliterator read a dead consonant plus a fresh syllable — "k setra"
    instead of "kshetra" — so the row stops answering to anything a reader
    types. Every character in Unicode's format category (Cf) goes, which also
    covers a BOM or a bidi mark pasted in from a PDF.
    """
    nfc = unicodedata.normalize("NFC", text)
    return "".join(ch for ch in nfc if unicodedata.category(ch) != "Cf")


def _search_fold(text: str) -> str:
    """Reduce Latin text to its sound classes. Lossy by design; never displayed."""
    folded = _fold_diacritics(_clean_for_search(text)).lower()
    folded = _NON_ALNUM_RE.sub(" ", folded)
    for src, dst in _SEARCH_SUBSTITUTIONS:
        folded = folded.replace(src, dst)
    folded = _ASPIRATE_RE.sub(r"\1", folded)
    folded = _GLIDE_RE.sub("b", folded)
    folded = _REPEAT_RE.sub(r"\1", folded)
    return " ".join(folded.split())


def search_fold(text: str) -> str:
    """The query side of :func:`matches_search`, for callers that need the tokens.

    :func:`matches_search` answers *whether* a string matches; a caller that has
    to say *where* — to cut a snippet around the matching span — needs the same
    folded tokens it compares against ``search_key``. Exposed rather than
    reimplemented so the two can never drift: a second fold would quietly accept
    or reject different strings than the matcher that decided there was a hit at
    all. See ``materials.content_api._phonetic_span``.
    """
    return _search_fold(text)


def search_key_uncached(text: str) -> str:
    """:func:`search_key` without touching the shared cache.

    For callers that fold strings from a corpus rather than from a fixed
    working set, and hold their own request-scoped cache instead. The shared
    cache below is sized for a few hundred short captions; a caller pushing one
    document's whole vocabulary through it would evict that working set on
    every request and keep the replacement resident forever. See
    ``materials.content_api.DocumentScan``.
    """
    from .indexing import has_devanagari

    cleaned = _clean_for_search(text)
    parts = [_search_fold(cleaned)]
    if has_devanagari(cleaned):
        parts.append(_search_fold(to_roman_colloquial(cleaned)))
    return " ".join(p for p in parts if p)


@lru_cache(maxsize=8192)
def search_key(text: str) -> str:
    """The form both a stored string and a typed query are reduced to.

    Carries the folded ORIGINAL — so English text still matches an English
    query — and, for Devanagari input, the folded colloquial romanization, which
    is what lets a Latin-keyboard query reach a Nepali caption.

    Cached because the inputs repeat and the output never changes: a listing
    filter runs this over the same few hundred captions on every keystroke, and
    transliteration is the expensive half. Measured on the largest report, 290
    captions cost ~23 ms uncached and effectively nothing on the second pass.
    Bounded at 8192 entries — captions are short, and the working set is one
    document's worth.

    **Pass short strings.** The bound is on entry COUNT, not bytes, so folding
    page-sized text here costs ~6 kB per slot and 8192 of them stay resident
    for the life of the process. Use :func:`search_key_uncached` for that.
    """
    return search_key_uncached(text)


def matches_search(haystack: str, query: str) -> bool:
    """Does ``haystack`` match ``query``, in script or in sound?

    Literal substring is tried first, so a Devanagari query — and an English
    query against English text — behaves exactly as a plain filter would and
    costs no transliteration at all.

    The phonetic pass is token-AND and order-free: every token of the query must
    appear somewhere in the key. Order-sensitive matching meant "ujuri badhi"
    found nothing while "badhi ujuri" found eighteen rows, which reads to a
    reader as the archive not holding it.
    """
    q = query.strip()
    if not q:
        return True
    if q.casefold() in haystack.casefold():
        return True

    tokens = [t for t in _search_fold(q).split() if t]
    if not tokens:
        return False
    key = search_key(haystack)
    return all(token in key for token in tokens)
