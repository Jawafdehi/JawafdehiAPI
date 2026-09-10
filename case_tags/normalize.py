"""Raw tag value -> canonical tag id.

Implements the normalization contract in
``jawafdehi-meta/docs/jawafdehi-search/design.md`` §12: normalize whitespace and
capitalization, correct approved spelling variants, match against aliases, map to a
canonical id.

Pure functions plus one DB lookup, kept apart from ``models`` so the string handling
is unit-testable without a database.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import Enum

# The Preeti-conversion artefact. Devanagari ो (U+094B) and ौ (U+094C) arrive
# DECOMPOSED as ा + े / ा + ै when text has been through a legacy Preeti->Unicode
# conversion. The result renders almost identically but is a different byte
# sequence, so it can never match a correctly typed query. Two values in the live
# corpus carry it -- `माछापाेखरी` and `सार्वजनिक सम्पत्ति हानी नाेकसानी`.
#
# NFC does NOT fix this: ा + े is not a canonical decomposition of ो, it is two
# distinct combining marks that happen to look like one. The repair has to be
# explicit, and it has to run BEFORE anything else compares strings.
DEVANAGARI_REPAIR: dict[str, str] = {
    "ाे": "ो",  # ा + े -> ो
    "ाै": "ौ",  # ा + ै -> ौ
}

# Stripped from the ends only. Interior punctuation is meaningful (`R.K. Sharma`),
# but a trailing full stop is a typo -- the corpus has both `Conflict of Interest`
# and `Conflict of Interest.`, which must collapse to one alias key.
_EDGE_PUNCTUATION = ".,;:!?\"'()[]{}"

_SEPARATORS = re.compile(r"[-_]+")
_WHITESPACE = re.compile(r"\s+")


def normalize(value: str) -> str:
    """Fold a raw tag value to its alias lookup key.

    Order matters. Devanagari repair runs first (before any comparison), separators
    become spaces before whitespace is collapsed (so ``land-deal`` and ``land deal``
    converge), and casefold runs last so it cannot interfere with the Devanagari
    handling above it.

    Casefold rather than lower: it is the Unicode-correct fold for caseless matching
    and is a no-op on Devanagari, which is unicameral.

    >>> normalize("  Public   Office Abuse ")
    'public office abuse'
    >>> normalize("Money-laundering")
    'money laundering'
    >>> normalize("Conflict of Interest.")
    'conflict of interest'
    """
    folded = unicodedata.normalize("NFC", value)
    for broken, repaired in DEVANAGARI_REPAIR.items():
        folded = folded.replace(broken, repaired)
    folded = _SEPARATORS.sub(" ", folded)
    folded = _WHITESPACE.sub(" ", folded).strip()
    folded = folded.strip(_EDGE_PUNCTUATION).strip()
    return folded.casefold()


class Resolution(Enum):
    """Why a lookup produced (or failed to produce) a tag."""

    CANONICAL = "canonical"
    """Resolved to an active tag."""

    RETIRED = "retired"
    """A value we deliberately removed from the vocabulary (`CIAA`, a money amount).

    Distinct from UNKNOWN on purpose. `?tags=CIAA` is a live URL today; after the
    cleanup it must be able to say "that filter no longer exists" rather than
    "unknown tag", which would read as a bug to anyone holding a bookmark.
    """

    UNKNOWN = "unknown"
    """Matches no alias at all."""


@dataclass(frozen=True)
class ResolvedTag:
    """The outcome of resolving one raw value."""

    resolution: Resolution
    tag_id: str | None = None
    reason: str = ""

    @property
    def is_canonical(self) -> bool:
        return self.resolution is Resolution.CANONICAL
