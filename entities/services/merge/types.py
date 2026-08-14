"""The merge's type guard: may these two entities be the same thing?

One rule, and deliberately only one. Of the thirty tokens in the entity vocabulary
(``entities.validation``) exactly one denotes a human, so a Person may only merge
with a Person. Everything else is allowed: a ``Place`` folding into an
``AdministrativeArea`` is the duplicate this endpoint exists to fix, and a
``Hospital`` is legitimately both an organization and a place.

Nothing here enumerates types. A token added to the vocabulary needs no edit — it is
simply not ``Person``, which is the safe answer.

``PERSON`` below is one literal, and it is a reference to the vocabulary rather than a
copy of it: ``test_the_person_token_is_the_one_the_vocabulary_defines`` fails in CI if
``entities.validation`` ever stops defining that token. A hand-maintained table of type
families could not be pinned that way — it drifts silently, and the symptom is a merge
refused with no explanation.
"""

from __future__ import annotations

from typing import Any, Dict, List

from jawafdehi_shared.entities.ids import parse_entity_iri

from entities.validation import JAWAFDEHI_NS

PERSON = "Person"


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


def types_of(doc: Dict[str, Any]) -> List[str]:
    """A document's @type tokens, normalized. Always a list, even for a single type."""
    atype = doc.get("@type")
    tokens = atype if isinstance(atype, list) else [atype]
    return [normalize_type_token(t) for t in tokens if isinstance(t, str)]


def is_person(doc: Dict[str, Any]) -> bool:
    return PERSON in types_of(doc)


def types_compatible(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """False only when one document is a Person and the other is not."""
    return is_person(a) == is_person(b)


def prefix_root(iri: str) -> str:
    """The first segment of an entity IRI's prefix — ``location/district`` → ``location``."""
    try:
        return parse_entity_iri(iri).prefix.split("/", 1)[0]
    except (ValueError, TypeError):
        return ""


def prefix_mismatch(survivor_iri: str, duplicate_iri: str) -> bool:
    """True when two IRIs sit under different top-level prefixes.

    Advisory only. Folding ``kalikot/…`` into ``location/district/…`` is exactly the
    cleanup this endpoint is for, so a mismatch warns and never refuses.
    """
    survivor_root, duplicate_root = prefix_root(survivor_iri), prefix_root(duplicate_iri)
    return bool(survivor_root) and bool(duplicate_root) and survivor_root != duplicate_root
