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

from typing import Any, Dict, List

from jawafdehi_shared.entities.ids import is_valid_entity_iri

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

    return doc


def primary_type(doc: Dict[str, Any]) -> str:
    """The promoted ``entity_type`` for a doc: the @type, joining a list with ','."""
    atype = doc.get("@type")
    if isinstance(atype, list):
        return ",".join(str(t) for t in atype)
    return str(atype)
