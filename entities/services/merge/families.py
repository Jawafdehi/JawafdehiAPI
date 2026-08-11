"""Type-family compatibility: is a merge between two entities the same kind of thing?

Exact ``@type`` equality would reject every real location duplicate we have — the
loose ``location/jhapa`` is a ``Place`` while ``location/district/jhapa-np0104`` is
an ``AdministrativeArea`` plus ``jawafdehi:District``. Families overlap instead, and
a type may sit in more than one (schema.org's Hospital is both an organization and
a place).
"""

from __future__ import annotations

from typing import Any, Dict

from entities.validation import JAWAFDEHI_NS

#: Wildcard: the open-world fallback @type, compatible with anything.
THING = "Thing"

FAMILIES: Dict[str, frozenset] = {
    "person": frozenset({"Person"}),
    "organization": frozenset(
        {
            "Organization",
            "GovernmentOrganization",
            "NGO",
            "Corporation",
            "EducationalOrganization",
            "Hospital",
            "Courthouse",
            "jawafdehi:PoliticalParty",
            "jawafdehi:Contractor",
            "jawafdehi:JudicialBody",
            "jawafdehi:InternationalOrganization",
        }
    ),
    "place": frozenset(
        {
            "Place",
            "AdministrativeArea",
            "CivicStructure",
            "Hospital",
            "Courthouse",
            "jawafdehi:Province",
            "jawafdehi:District",
            "jawafdehi:MetropolitanCity",
            "jawafdehi:SubMetropolitanCity",
            "jawafdehi:Municipality",
            "jawafdehi:RuralMunicipality",
            "jawafdehi:Ward",
            "jawafdehi:ElectoralConstituency",
        }
    ),
    "work": frozenset(
        {
            "CreativeWork",
            "Project",
            "Service",
            "GovernmentService",
            "jawafdehi:DevelopmentProject",
        }
    ),
}

FAMILY_NAMES: frozenset = frozenset(FAMILIES)


def normalize_type_token(token: str) -> str:
    """Reduce any accepted @type spelling to its bare or ``jawafdehi:`` form."""
    if not isinstance(token, str) or not token:
        return ""
    if token.startswith(JAWAFDEHI_NS):
        return "jawafdehi:" + token[len(JAWAFDEHI_NS):]
    if token.startswith("jawafdehi:"):
        return token
    if token.startswith("schema:"):
        return token.split(":", 1)[1]
    if token.startswith("https://schema.org/") or token.startswith("http://schema.org/"):
        return token.rsplit("/", 1)[-1]
    return token


def families_for(doc: Dict[str, Any]) -> frozenset:
    """The families a document's @type belongs to (empty if none are known)."""
    atype = doc.get("@type")
    tokens = atype if isinstance(atype, list) else [atype]
    found: set = set()
    for raw in tokens:
        if not isinstance(raw, str):
            continue
        token = normalize_type_token(raw)
        if token == THING:
            return FAMILY_NAMES
        for family, members in FAMILIES.items():
            if token in members:
                found.add(family)
    return frozenset(found)


def families_compatible(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """True when the two documents share at least one type family."""
    return bool(families_for(a) & families_for(b))
