"""Minimal validation for stored schema.org JSON-LD entity documents.

CLEAN-SLATE design (2026-06-28): NES stores raw schema.org JSON-LD keyed by an
``@id`` IRI. Validation is intentionally MINIMAL — there are no per-type Pydantic
models any more:

1. ``@type`` is a known schema.org / ``jawafdehi:`` type,
2. ``@id`` is a valid canonical entity IRI (``jawafdehi_shared.entities.ids``),
3. ``name`` is present (a string or a non-empty language map).

Everything else in the document is free-form JSON-LD and is stored verbatim.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from jawafdehi_shared.entities.ids import is_valid_entity_iri

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# IAST / machine-transliteration detection for English names
# ---------------------------------------------------------------------------
# Historic bulk loads populated ``name.en`` with a raw character-level
# transliteration of the Devanagari instead of a real English name — either
# academic IAST (diacritics: नारायणी → "Nārāyaṇī") or Harvard-Kyoto/ITRANS
# (long vowels marked by mid-word capitals: नारायणी अस्पताल → "nArAyaNI
# aspatAla"). A proper English name TRANSLATES generic nouns (अस्पताल→Hospital)
# and romanizes proper nouns cleanly ("Narayani Hospital"). We can't verify the
# latter here, but we CAN cheaply spot the machine-transliteration signature and
# warn, so a regression (a new load reintroducing it) is visible in the logs
# rather than silent. This is observability only — it never blocks a write.

# Academic IAST diacritics / accented Latin used by transliteration schemes.
_IAST_DIACRITICS = re.compile(r"[āīūṛṝḷḹṅñṭḍṇśṣṃḥĀĪŪṚṜḶḸṄÑṬḌṆŚṢṂḤ]")
# Harvard-Kyoto / ITRANS long-vowel + anusvara markers, matched PER TOKEN: an
# otherwise all-lowercase token carrying an embedded/final capital A I U E M R
# (aspatAla, baiMka, nArAyaNI, padmA, kampanI). Anchoring to a lowercase-run
# start means real English mixed-case tokens (CamelCase "AsiaInfo", "McMahon",
# "iPhone"; Titlecase; ALLCAPS "UOB") never match — chosen to be false-positive
# free because this drives a log warning, not a hard rejection.
_HK_TOKEN = re.compile(r"^[a-z]+[AIU]([a-z]|$)")
# Anusvara/nasal tilde between letters (sA~da, kAThamADau~).
_HK_TILDE = re.compile(r"[a-zA-Z]~[a-zA-Z]")
_TOKEN_SPLIT = re.compile(r"[\s,()]+")


def looks_like_iast(value: Any) -> bool:
    """True if a name string carries a machine-transliteration signature.

    Detects academic IAST diacritics and Harvard-Kyoto/ITRANS capital-vowel
    markers — the hallmark of a romanized-not-translated ``en`` name. Used for a
    log-only warning during validation; not a hard rule. Tuned to be
    false-positive free on legitimate English/mixed-case names.
    """
    if not isinstance(value, str):
        return False
    if _IAST_DIACRITICS.search(value) or _HK_TILDE.search(value):
        return True
    return any(_HK_TOKEN.search(tok) for tok in _TOKEN_SPLIT.split(value))

# ---------------------------------------------------------------------------
# Known @type vocabulary
# ---------------------------------------------------------------------------
# The schema.org types the platform emits/accepts for entities (the
# nes-schema-org.md mapping), plus a handful of generic schema.org types and the
# ``jawafdehi:`` extension types. A bare ``Thing`` is allowed as the open-world
# fallback. ``@type`` may be a single string or a list; every member must be
# known. Keep this in sync with the mapping doc + jawafdehi: namespace.

KNOWN_SCHEMAORG_TYPES = frozenset(
    {
        "Thing",
        # People & organizations
        "Person",
        "Organization",
        "GovernmentOrganization",
        "NGO",
        "Corporation",
        "EducationalOrganization",
        "Hospital",
        # Places / administrative areas
        "Place",
        "AdministrativeArea",
        "Courthouse",
        "CivicStructure",
        # Projects / services / works
        "Project",
        "GovernmentService",
        "CreativeWork",
        "Service",
    }
)

# Nepal-specific extension types carried under the jawafdehi: namespace. Accepted
# both as the bare ``jawafdehi:`` CURIE and as the full namespace IRI.
JAWAFDEHI_NS = "https://jawafdehi.org/ns#"
KNOWN_JAWAFDEHI_TYPES = frozenset(
    {
        "PoliticalParty",
        "Contractor",
        "JudicialBody",
        "InternationalOrganization",
        "Province",
        "District",
        "MetropolitanCity",
        "SubMetropolitanCity",
        "Municipality",
        "RuralMunicipality",
        "Ward",
        "ElectoralConstituency",
        "DevelopmentProject",
    }
)


class JsonLdValidationError(ValueError):
    """Raised when a JSON-LD entity document fails minimal validation."""


def _is_known_type(t: str) -> bool:
    if not isinstance(t, str) or not t:
        return False
    if t in KNOWN_SCHEMAORG_TYPES:
        return True
    if t.startswith("jawafdehi:"):
        return t.split(":", 1)[1] in KNOWN_JAWAFDEHI_TYPES
    if t.startswith(JAWAFDEHI_NS):
        return t[len(JAWAFDEHI_NS):] in KNOWN_JAWAFDEHI_TYPES
    # schema.org may be written with a prefix/IRI form, e.g. "schema:Person".
    if t.startswith("schema:"):
        return t.split(":", 1)[1] in KNOWN_SCHEMAORG_TYPES
    if t.startswith("https://schema.org/") or t.startswith("http://schema.org/"):
        return t.rsplit("/", 1)[-1] in KNOWN_SCHEMAORG_TYPES
    return False


def _name_present(name: Any) -> bool:
    """A ``name`` is present if it's a non-empty string or a non-empty language map."""
    if isinstance(name, str):
        return bool(name.strip())
    if isinstance(name, dict):
        return any(isinstance(v, str) and v.strip() for v in name.values())
    return False


def validate_jsonld_entity(doc: Any) -> Dict[str, Any]:
    """Validate a schema.org JSON-LD entity document (minimal rules).

    Returns the document unchanged on success; raises ``JsonLdValidationError``
    on the first failure.
    """
    if not isinstance(doc, dict):
        raise JsonLdValidationError("Entity document must be a JSON object.")

    # @id — a valid canonical entity IRI.
    iri = doc.get("@id")
    if not isinstance(iri, str) or not is_valid_entity_iri(iri):
        raise JsonLdValidationError(
            f"@id must be a valid entity IRI (https://.../entity/<prefix>/<slug>), got {iri!r}"
        )

    # @type — a known schema.org / jawafdehi: type (string or list of strings).
    atype = doc.get("@type")
    types: List[Any] = atype if isinstance(atype, list) else [atype]
    if not types or not all(_is_known_type(t) for t in types):
        raise JsonLdValidationError(
            f"@type must be a known schema.org/jawafdehi type, got {atype!r}"
        )

    # name — present.
    if not _name_present(doc.get("name")):
        raise JsonLdValidationError(
            "name is required (a string or a non-empty language map)."
        )

    # name.en quality — log-only warning if it carries a machine-transliteration
    # signature (IAST diacritics / Harvard-Kyoto capital-vowel markers). This is
    # NOT a hard rule: it never blocks the write, only surfaces a likely-bad
    # English name in the logs so a regression is caught. A backfill fixes the
    # value properly; see docs / the entity-en normalization mutation set.
    _warn_if_iast_en(doc)

    return doc


def _warn_if_iast_en(doc: Dict[str, Any]) -> None:
    """Emit a warning when ``name.en`` looks like a raw transliteration."""
    name = doc.get("name")
    en = name.get("en") if isinstance(name, dict) else None
    if looks_like_iast(en):
        logger.warning(
            "entity %s has a machine-transliterated name.en (%r) — likely IAST/"
            "Harvard-Kyoto, not a real English name; needs backfill.",
            doc.get("@id"),
            en,
        )


def primary_type(doc: Dict[str, Any]) -> str:
    """The promoted ``entity_type`` for a doc: the @type, joining a list with ','."""
    atype = doc.get("@type")
    if isinstance(atype, list):
        return ",".join(str(t) for t in atype)
    return str(atype)
