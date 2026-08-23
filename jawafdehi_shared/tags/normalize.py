"""Mechanical hygiene for tag strings — the one shared ``normalize_tag``.

Pure and Django-free by contract (asserted by a subprocess import in the tests):
no vocabulary lookup, no I/O, no models. It is the step that runs BEFORE any alias
or canonical-id resolution, and it does only the defects that need **zero
editorial judgement**. Anything requiring a human decision — which spelling is
correct, which variant is canonical, which concept two words share — belongs to
the controlled vocabulary and its alias table, not here.

Why a separate function at all, when tag strings are never normalized anywhere
today (``cases/search_index.py:239`` is literally ``keywords = list(tags)``): the
live corpus carries 144 distinct tags over 82 cases, and a measurable share of
that count is not vocabulary at all but the same term stored twice. ``Ncell`` and
``ncell`` are two tags, so a filter on either finds half the cases.

The defects this repairs, all measured in the live corpus
(``research/corpus-analysis.md`` §7):

* **casing collisions** — ``Ncell``/``ncell``, ``Tax Evasion``/``tax evasion``,
  ``Illegal Enrichment``/``Illegal enrichment``;
* **trailing punctuation** — ``Abuse of Power.``, ``Conflict of Interest.``;
* **a double space** — ``Hulak Saving  Bank Case``;
* **a Devanagari encoding fault** in two tags — see below, this is the dangerous
  one;
* **mixed-script digits** — ``24९`` is Latin 2, Latin 4, Devanagari ९.

THE ENCODING FAULT IS THE REASON THIS MODULE IS NOT A ONE-LINER. Two live tags
spell ``ो`` (U+094B) as ``ा`` + ``े`` (U+093E U+0947) — a Preeti-keyboard artefact.
It renders almost identically, so it passed editorial review, but the bytes
differ: **a reader searching माछापोखरी never finds माछापाेखरी.** And
``unicodedata.normalize`` DOES NOT REPAIR IT — U+094B has no canonical
decomposition at all (verified at codepoint level in the tests), so ा+े is not a
decomposition of ो and NFC is a no-op on it. Reaching for NFC here looks like a
fix and does nothing. It must be an explicit substitution (policy §7.2,
decision D12).

**Step order is load-bearing** and is fixed by the task spec:

1. the Devanagari substitutions — before anything else, so every later step and
   the eventual alias lookup see the repaired form. Do this after an alias lookup
   and ``माछापाेखरी`` misses its entry;
2. NFC — for the (real, different) cases NFC does fix;
3. trim + collapse internal whitespace;
4. strip trailing punctuation — also before any alias lookup, or
   ``Abuse of Power.`` misses its entry;
5. fold Devanagari digits to ASCII;
6. lowercase — LAST, and only after the acronym allow-list, because blind
   case-folding turns the sector tag ``IT`` into the English word "it".

Deliberately NOT done here, each for a stated reason:

* **§7.3 spelling corrections** (हानी→हानि, नाेकसानी→नोक्सानी, घूस→घुस,
  सफाई→सफाइ). These are editorial judgements measured against attested usage in
  published case texts, not mechanical rules. They belong in the alias table.
  This module repairs the ENCODING of ``नाेकसानी`` and leaves its SPELLING alone.
* **kebab-casing / slugification.** ``land-deal`` stays as it is; minting slugs is
  the vocabulary's job (policy §7.1 rule 1), and doing it here would silently
  invent canonical ids.
* **zero-width character stripping.** design.md §7 lists it for QUERY
  normalization, but ZWNJ/ZWJ can be semantically load-bearing inside Devanagari
  text, so removing them from stored values needs its own decision rather than a
  quiet inclusion in a hygiene pass.
* **fuzzy or nearest-match anything.** A wrong silent mapping is worse than an
  unresolved value.

Relationship to :func:`search.analytics.normalize_query`, which shares this
module's NFC + trim + lower + collapse core: that one buckets free-text QUERIES
for analytics, where folding every acronym is correct and desirable. This one
normalizes STORED tag values, where ``IT`` must survive and the encoding fault
must be repaired. They are deliberately separate; merging them would either break
analytics buckets or lose the acronym allow-list. Documented at both sites.
"""

from __future__ import annotations

import re
import unicodedata

# policy §7.2 / decision D12. The ONLY two substitutions, spelled as explicit
# codepoints because the source and target render almost identically — a literal
# here would be unreviewable, and a copy-paste through an editor that "helpfully"
# normalizes would silently turn this into an identity map.
_DEVANAGARI_FAULTS: tuple[tuple[str, str], ...] = (
    ("ाे", "ो"),  # ा + े  ->  ो
    ("ाै", "ौ"),  # ा + ै  ->  ौ
)

# Devanagari digits ०..९ (U+0966..U+096F) -> ASCII. Folded UNCONDITIONALLY, not
# only when a token mixes scripts: "one form" needs a single target, and ASCII is
# the only direction consistent with policy §7.1 rule 1 (ASCII slugs) and with the
# corpus already carrying the all-Latin spelling of the same statute (``249``
# alongside ``24९``). Neither NFC nor NFKC does this — both leave ९ alone, which is
# why the table has to exist at all (verified in the tests).
#
# KNOWN DUPLICATE, and the sixth copy of this table in the tree — the others are
# ``jawafdehi_shared/dates.py``, ``courts/normalize.py`` (public, dict form),
# ``materials/sourcing/ag/shaper.py``, ``casework/enrich_missing_bigo.py`` and
# ``casework/enrich_timeline.py`` (plus the reverse direction in
# ``casework/news_search.py``). Spelled in the same ``maketrans`` form as the
# majority of those so a future consolidation into one shared constant is a
# mechanical edit; that consolidation spans four apps and is not this task's to
# make. Inventory recorded here so whoever does it does not have to re-find them.
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Trailing punctuation only: the full stop and the Devanagari danda (policy §7.1
# rule 3). INTERNAL punctuation is left alone — ``K.P. Sharma Oli`` keeps its
# stops, and stripping those would need a name-parsing rule this module has no
# business having.
_TRAILING_PUNCTUATION = ".।"

_WHITESPACE_RE = re.compile(r"\s+")

# Tags that must survive case-folding. HUMAN-SUPPLIED vocabulary, taken verbatim
# from the task spec — deliberately NOT inferred from the corpus. You cannot tell
# from the string ``nphl`` that it is the National Public Health Laboratory rather
# than a typo, and guessing would be exactly the silent-wrong-mapping failure this
# programme exists to stop. Additions are an editorial act: ask, do not extend.
#
# Matched case-INSENSITIVELY, emitting the spelling below. That is what collapses
# the live ``nphl`` (×4, lowercase) onto ``NPHL`` — the same casing-collision
# defect as Ncell/ncell. The accepted cost is that a bare tag "it" would be read
# as the acronym; a one-word tag "it" is not a plausible tag, whereas the sector
# tag ``IT`` is live (×2).
ACRONYMS: frozenset[str] = frozenset({"IT", "NITC", "CIAA", "RSP", "NPHL"})

_ACRONYM_BY_FOLDED: dict[str, str] = {a.lower(): a for a in ACRONYMS}


def normalize_tag(value: str) -> str:
    """Return the mechanically-normalized form of one tag string.

    Idempotent (``normalize_tag(normalize_tag(x)) == normalize_tag(x)``) so it is
    safe in front of the alias lookup that will consume it. Returns ``""`` for a
    blank or punctuation-only value; raises on a non-string rather than coercing,
    because the write-validation layer above needs to reject bad input with a 400
    and cannot do that if this quietly stringifies whatever it is handed.
    """
    # Explicit, because ``if not value`` alone would quietly accept ``None`` and
    # return "" — and an empty tag does not fail, it DISAPPEARS from the case. That
    # is the silent-drop behaviour policy §10 calls unacceptable ("a caseworker
    # would believe the tag had saved"). The MCP JSON-Patch write path carries an
    # untyped ``value``, so a null really can arrive here.
    if not isinstance(value, str):
        raise TypeError(f"normalize_tag expects a str, got {type(value).__name__}")
    if not value:
        return ""

    # 1. The encoding fault, before anything else (see the module docstring).
    for fault, repair in _DEVANAGARI_FAULTS:
        value = value.replace(fault, repair)

    # 2. NFC, for the composed/decomposed cases it genuinely does fix.
    value = unicodedata.normalize("NFC", value)

    # 3. Trim, and collapse internal runs of whitespace to one space.
    value = _WHITESPACE_RE.sub(" ", value).strip()

    # 4. Strip trailing punctuation, then re-trim: "Abuse of Power ." leaves a
    #    space behind, and a value that is punctuation all the way down
    #    (``"."``) must end up empty rather than a stray space.
    value = value.rstrip(_TRAILING_PUNCTUATION).strip()

    # 5. Devanagari digits -> ASCII.
    value = value.translate(_DEVANAGARI_DIGITS)

    # 6. Case-fold LAST, and never for an allow-listed acronym.
    #
    # ``.lower()`` rather than ``.casefold()``: no value in the measured corpus
    # (English + Devanagari, and Devanagari is caseless) differs between the two,
    # and ``.lower()`` is what the sibling ``normalize_query`` uses, so the two
    # stay comparable. casefold's extra folds — ß→ss and friends — would silently
    # rewrite letters nobody asked it to touch.
    acronym = _ACRONYM_BY_FOLDED.get(value.lower())
    if acronym is not None:
        return acronym
    return value.lower()
