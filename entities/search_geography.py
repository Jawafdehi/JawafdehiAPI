"""Subject geography (district / province) for the unified search.

This module answers ONE question: which Nepali districts and provinces is a
record *about*? It is the second derived facet on the entity document, built
the same way as ``entities.search_visibility``'s ``case_count`` — computed from
the case binds at index time, promoted onto the search document, filtered at
query time.

WHY NOT THE ENTITY'S OWN ADDRESS. Only 4% of case-cited entities carry any
geography at all: ``containedInPlace`` is populated by the bulk importers
(hospitals, wards, places) and absent from everything
``casework.enrich_related_entities`` creates, which is most of what a published
case cites. A facet built on the entity's own address would be empty for the
corpus that is actually visible. The CASE knows where it happened — 95.1% of
published cases bind a ``location`` entity — so the geography flows the other
way: from the case, onto the entities it cites.

THE PLACE GRAPH. A ``location`` bind points at one of three canonical kinds,
and each resolves to a (district, province) pair:

* ``location/district/*``  — 77 of them, each carrying an OCHA p-code;
* ``location/localunit/*`` — 754, each ``containedInPlace`` its district;
* ``location/province/*``  — 7, province only, no district.

A bind may also point at an ad-hoc place the enricher minted from free text
(``location/jamal-kathmandu``, ``location/the-hague-fdd146``) or — a data error
— at an organization or a person. Those resolve to NOTHING and are skipped:
53 of 3,693 location binds (1.4%) today. Guessing a district from a slug is
exactly the kind of plausible-but-wrong bucket this module must not produce.

CANONICAL SPELLING. The district vocabulary is ``courts.geography``'s, NOT the
entity store's, because the ``district`` facet already ships 27,986 court-case
documents using those spellings and two spellings of one district would split
the bucket — which is the mess this facet exists to avoid. The two tables
disagree on 8 of 77 names, so :data:`PCODE_DISTRICT` is keyed on the OCHA
p-code rather than the name. That is not fussiness: entity ``Nawalparasi East``
is canonical ``Nawalpur`` while entity ``Nawalparasi West`` is canonical
``Nawalparasi``, so name-based normalization gets the split districts exactly
backwards. The p-code cannot.

PROVINCE IS DERIVED FROM THE DISTRICT, never read from the province entity, so
a document's district and province can never disagree. The province entities
are only consulted when a case binds a province DIRECTLY and names no district.

FAILURE POSTURE, split by blast radius exactly as ``search_visibility`` splits
it: :func:`case_locations` (bulk, feeds a whole reindex) raises, because a
silent empty map would strip the facet off the entire corpus in one alias swap.
:func:`entity_locations` (live, one document) logs and returns empty, because
one stale document is repaired by the next reindex and aborting the save's
indexing is worse.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from django.core.cache import cache

logger = logging.getLogger(__name__)

#: IRI marker for the place entities. Everything under it is a candidate for
#: the place graph; everything outside it can never be a location.
LOCATION_PREFIX = "/entity/location/"

#: Cache key for the resolved place graph. Versioned so a shape change cannot
#: read a payload written by the old definition.
PLACES_CACHE_KEY = "entities:place_geography:v1"

#: Cache TTL for the place graph. Long, because the 838 place entities change
#: approximately never — they are the 2015 federal structure. The authoritative
#: invalidation is still :func:`clear_cache`.
PLACES_CACHE_TTL_SECONDS = 3600

#: Maximum ``containedInPlace`` hops followed when resolving a place. The real
#: graph is at most localunit -> district -> province, so 4 is slack; the bound
#: exists to stop a cyclic or self-referential record, not to express the depth.
MAX_PLACE_DEPTH = 4


#: OCHA p-code -> the district name as ``courts.geography.DISTRICT_PROVINCE``
#: spells it. All 77 districts, keyed on the p-code because 8 of the names
#: differ between the two sources and 4 of those are the split districts, where
#: a name match is actively wrong (see the module docstring).
#: ``entities/tests/test_search_geography.py`` pins this against
#: ``DISTRICT_PROVINCE`` in both directions, so the tables cannot drift.
PCODE_DISTRICT: dict[str, str] = {
    # Bagmati (13)
    "NP0326": "Bhaktapur",
    "NP0335": "Chitwan",
    "NP0330": "Dhading",
    "NP0322": "Dolakha",
    "NP0327": "Kathmandu",
    "NP0324": "Kavrepalanchowk",  # OCHA: Kavrepalanchok
    "NP0325": "Lalitpur",
    "NP0331": "Makwanpur",
    "NP0328": "Nuwakot",
    "NP0321": "Ramechhap",
    "NP0329": "Rasuwa",
    "NP0320": "Sindhuli",
    "NP0323": "Sindhupalchowk",  # OCHA: Sindhupalchok
    # Gandaki (11)
    "NP0443": "Baglung",
    "NP0436": "Gorkha",
    "NP0439": "Kaski",
    "NP0437": "Lamjung",
    "NP0438": "Manang",
    "NP0445": "Mustang",
    "NP0444": "Myagdi",
    "NP0447": "Nawalpur",  # OCHA: Nawalparasi East
    "NP0442": "Parbat",
    "NP0441": "Syangja",
    "NP0440": "Tanahun",
    # Karnali (10)
    "NP0661": "Dailekh",
    "NP0662": "Dolpa",
    "NP0666": "Humla",
    "NP0660": "Jajarkot",
    "NP0663": "Jumla",
    "NP0664": "Kalikot",
    "NP0665": "Mugu",
    "NP0652": "Rukum",  # OCHA: Rukum West
    "NP0653": "Salyan",
    "NP0659": "Surkhet",
    # Koshi (14)
    "NP0110": "Bhojpur",
    "NP0107": "Dhankuta",
    "NP0103": "Ilam",
    "NP0104": "Jhapa",
    "NP0112": "Khotang",
    "NP0105": "Morang",
    "NP0113": "Okhaldhunga",
    "NP0102": "Panchthar",
    "NP0109": "Sankhuwasabha",
    "NP0111": "Solukhumbu",
    "NP0106": "Sunsari",
    "NP0101": "Taplejung",
    "NP0108": "Therathum",  # OCHA: Terhathum
    "NP0114": "Udayapur",
    # Lumbini (12)
    "NP0550": "Arghakhanchi",
    "NP0557": "Banke",
    "NP0558": "Bardiya",
    "NP0556": "Dang",
    "NP0551": "Gulmi",
    "NP0549": "Kapilbastu",  # OCHA: Kapilvastu
    "NP0547": "Nawalparasi",  # OCHA: Nawalparasi West
    "NP0546": "Palpa",
    "NP0555": "Pyuthan",
    "NP0554": "Rolpa",
    "NP0552": "Rukumkot",  # OCHA: Rukum East
    "NP0548": "Rupandehi",
    # Madhesh (8)
    "NP0233": "Bara",
    "NP0217": "Dhanusha",
    "NP0218": "Mahottari",
    "NP0234": "Parsa",
    "NP0232": "Rautahat",
    "NP0215": "Saptari",
    "NP0219": "Sarlahi",
    "NP0216": "Siraha",
    # Sudurpashchim (9)
    "NP0769": "Achham",
    "NP0774": "Baitadi",
    "NP0767": "Bajhang",
    "NP0768": "Bajura",
    "NP0773": "Dadeldhura",
    "NP0775": "Darchula",
    "NP0770": "Doti",
    "NP0771": "Kailali",
    "NP0772": "Kanchanpur",
}

#: OCHA p-code -> province name as ``courts.geography`` spells it. Consulted
#: ONLY for a case that binds a province directly; a province reached through a
#: district comes from ``DISTRICT_PROVINCE`` instead, so the two can never
#: disagree. The entity names carry a " Province" suffix the court facet does
#: not, which is why this is a table and not a string operation.
PCODE_PROVINCE: dict[str, str] = {
    "NP01": "Koshi",
    "NP02": "Madhesh",
    "NP03": "Bagmati",
    "NP04": "Gandaki",
    "NP05": "Lumbini",
    "NP06": "Karnali",
    "NP07": "Sudurpashchim",
}


class GeographyLookupError(RuntimeError):
    """The subject geography could not be read from the databases.

    Raised only by the bulk path (:func:`case_locations`), where answering
    "nothing has a location" would strip the facet off every document at once.
    """


def _pcode(data: dict[str, Any]) -> str | None:
    """The OCHA p-code of a place record, upper-cased, or ``None``.

    Reads the ``identifier`` list rather than the IRI slug: the slug ends in a
    lower-cased p-code today, but it is a display artefact and nothing enforces
    it, while ``propertyID: "ocha-pcode"`` is what the place importer writes.
    """
    identifiers = data.get("identifier")
    if isinstance(identifiers, dict):
        identifiers = [identifiers]
    for ident in identifiers or []:
        if not isinstance(ident, dict):
            continue
        if ident.get("propertyID") != "ocha-pcode":
            continue
        value = ident.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _parent_iri(data: dict[str, Any]) -> str | None:
    """The ``containedInPlace`` IRI of a place record, or ``None``.

    Tolerates both shapes the store holds: a node reference
    (``{"@id": ...}``) and a bare IRI string.
    """
    parent = data.get("containedInPlace")
    if isinstance(parent, dict):
        parent = parent.get("@id")
    if isinstance(parent, str) and parent.strip():
        return parent.strip()
    return None


def _build_place_index(records: Iterable[tuple[str, dict[str, Any]]]) -> dict[str, list[str | None]]:
    """``{place IRI: [district, province]}`` over the given place records.

    Two passes. The first reads the places that resolve on their own — a
    district (p-code -> canonical name -> its province) or a province (p-code
    -> canonical name, no district). The second walks every remaining place up
    ``containedInPlace`` until it lands on one of those, which is how a local
    unit inherits its district.

    A place that resolves to neither is simply absent from the result. That is
    the ad-hoc free-text location, and it must stay unresolved rather than be
    guessed at.
    """
    from courts.geography import DISTRICT_PROVINCE

    parents: dict[str, str] = {}
    resolved: dict[str, list[str | None]] = {}

    for iri, data in records:
        parent = _parent_iri(data)
        if parent:
            parents[iri] = parent
        code = _pcode(data)
        if not code:
            continue
        district = PCODE_DISTRICT.get(code)
        if district is not None:
            # The district table is pinned against DISTRICT_PROVINCE by test, so
            # this lookup cannot miss; .get keeps a drifted table from raising
            # inside a reindex.
            resolved[iri] = [district, DISTRICT_PROVINCE.get(district)]
            continue
        province = PCODE_PROVINCE.get(code)
        if province is not None:
            resolved[iri] = [None, province]

    for iri in parents:
        if iri in resolved:
            continue
        seen = {iri}
        cursor = parents.get(iri)
        for _ in range(MAX_PLACE_DEPTH):
            if cursor is None or cursor in seen:
                break
            if cursor in resolved:
                resolved[iri] = list(resolved[cursor])
                break
            seen.add(cursor)
            cursor = parents.get(cursor)

    return resolved


def place_index(*, refresh: bool = False) -> dict[str, list[str | None]]:
    """``{place IRI: [district, province]}`` for every place in the entity store.

    Cached (:data:`PLACES_CACHE_TTL_SECONDS`): ~838 rows that change with the
    federal structure, i.e. never, and every reindex and every live entity save
    needs the same map.

    Reads only ``/entity/location/`` rows, which is what keeps this one narrow
    query rather than a scan of 204k entities.
    """
    if not refresh:
        cached = cache.get(PLACES_CACHE_KEY)
        if cached is not None:
            return cached
    from django.db.models import Q

    from entities.models import StoredEntity

    # Filter on the INDEXED ``prefix`` column, not ``iri__contains``. The latter
    # is a leading-wildcard LIKE, which no btree index can serve — it would scan
    # all ~204k entity rows on every cache miss, including from the live
    # ``post_save`` path. ``prefix`` is ``db_index=True`` and a ``startswith``
    # stays sargable.
    #
    # Two arms rather than ``startswith("location")``: the bare ``location``
    # prefix is real (126 rows) and a lone ``startswith`` would also match a
    # hypothetical ``locationfoo``. The junk top-level prefixes that look like
    # places but are not under ``location/`` (``localunit``, ``place``,
    # ``kalikot``) are excluded here exactly as they were before — they carry no
    # p-code and resolved to nothing anyway.
    rows = (
        StoredEntity.objects.filter(
            Q(prefix="location") | Q(prefix__startswith="location/"),
            is_deleted=False,
        )
        .values_list("iri", "data")
        .iterator()
    )
    index = _build_place_index((iri, data or {}) for iri, data in rows)
    cache.set(PLACES_CACHE_KEY, index, PLACES_CACHE_TTL_SECONDS)
    return index


def _merge(
    target: tuple[set[str], set[str]], resolved: list[str | None] | None
) -> None:
    """Fold one resolved place into a (districts, provinces) accumulator."""
    if not resolved:
        return
    district, province = resolved[0], resolved[1]
    if district:
        target[0].add(district)
    if province:
        target[1].add(province)


def _sorted(pair: tuple[set[str], set[str]]) -> tuple[list[str], list[str]]:
    """Accumulator -> the document's field values.

    SORTED, so a document's bytes do not depend on set iteration order. An
    unstable field would make every reindex rewrite every document and would
    turn a no-op reconcile into a diff.
    """
    return sorted(pair[0]), sorted(pair[1])


def case_location_iris() -> dict[int, set[str]]:
    """``{case id: location-bind IRIs}`` for PUBLISHED cases only.

    PUBLISHED-only mirrors the visibility gate and the case index's own rule: a
    draft case's location is not public information.
    """
    from cases.models import CaseEntityRelationship, CaseState, RelationshipType

    rows = CaseEntityRelationship.objects.filter(
        case__state=CaseState.PUBLISHED,
        relationship_type=RelationshipType.LOCATION,
    ).values_list("case_id", "nes_id")
    out: dict[int, set[str]] = {}
    for case_id, nes_id in rows:
        if nes_id:
            out.setdefault(case_id, set()).add(nes_id)
    return out


def case_locations() -> dict[str, tuple[list[str], list[str]]]:
    """``{entity IRI: (districts, provinces)}`` for every cited entity.

    The BULK path, loaded ONCE by ``reindex_entities``: three queries and the
    cached place index, rather than a per-document resolution over 204k rows.

    An entity cited by cases in several districts carries all of them — the
    field is multi-valued, because "which districts is this person implicated
    in" has more than one answer and picking one would be a lie.

    Raises :class:`GeographyLookupError` if either database cannot be read.
    """
    from cases.models import CaseEntityRelationship, CaseState

    try:
        places = place_index()
        locations = case_location_iris()
        binds = CaseEntityRelationship.objects.filter(
            case__state=CaseState.PUBLISHED
        ).values_list("nes_id", "case_id")
    except Exception as exc:  # noqa: BLE001 — re-raised as a typed error below.
        raise GeographyLookupError(
            "could not read the published location binds for the geography facet"
        ) from exc

    try:
        accumulator: dict[str, tuple[set[str], set[str]]] = {}
        for nes_id, case_id in binds:
            if not nes_id:
                continue
            target = accumulator.setdefault(nes_id, (set(), set()))
            for place_iri in locations.get(case_id, ()):
                _merge(target, places.get(place_iri))
    except Exception as exc:  # noqa: BLE001 — same bulk posture as above.
        raise GeographyLookupError(
            "could not resolve the published location binds for the geography facet"
        ) from exc

    return {iri: _sorted(pair) for iri, pair in accumulator.items()}


def locations_for_iris(iris: Iterable[str]) -> dict[str, tuple[list[str], list[str]]]:
    """``{entity IRI: (districts, provinces)}`` for a SMALL, known set of IRIs.

    The batch form of :func:`entity_locations`, for ``cases.signals``' fan-out:
    a location bind on a 26-entity case re-indexes all 26, and resolving each
    one on its own would issue two queries per entity inside a commit hook.
    This is two queries for the whole list however long it is.

    Best-effort like the single-entity path — an empty dict on failure, which
    the caller renders as "no geography" per entity and the next reindex
    repairs. IRIs absent from the result have no published citation and so no
    geography; the caller must treat absent as ``([], [])``, not as "unknown".
    """
    wanted = [iri for iri in dict.fromkeys(iris) if iri]
    if not wanted:
        return {}
    try:
        from cases.models import CaseEntityRelationship, CaseState

        rows = CaseEntityRelationship.objects.filter(
            case__state=CaseState.PUBLISHED, nes_id__in=wanted
        ).values_list("nes_id", "case_id")
        cases_by_iri: dict[str, set[int]] = {}
        for nes_id, case_id in rows:
            cases_by_iri.setdefault(nes_id, set()).add(case_id)
        if not cases_by_iri:
            return {}
        every_case = set().union(*cases_by_iri.values())
        places = place_index()
        by_case: dict[int, tuple[set[str], set[str]]] = {}
        for case_id, place_iri in _location_binds_for_cases(every_case):
            target = by_case.setdefault(case_id, (set(), set()))
            _merge(target, places.get(place_iri))
        out: dict[str, tuple[list[str], list[str]]] = {}
        for iri, case_ids in cases_by_iri.items():
            target: tuple[set[str], set[str]] = (set(), set())
            for case_id in case_ids:
                found = by_case.get(case_id)
                if found:
                    target[0].update(found[0])
                    target[1].update(found[1])
            out[iri] = _sorted(target)
        return out
    except Exception:  # noqa: BLE001 — best-effort; the reindex repairs it.
        logger.warning(
            "locations_for_iris failed for %d entities; indexing with no geography",
            len(wanted),
            exc_info=True,
        )
        return {}


def entity_locations(iri: str | None) -> tuple[list[str], list[str]]:
    """``(districts, provinces)`` for one entity. The LIVE path.

    Logs and returns empty on a database failure rather than raising: the
    caller is best-effort indexing from a ``post_save``, one document with a
    missing facet value is repaired by the next reindex, and raising would
    abort the whole save's indexing. Mirrors
    ``entities.search_visibility.entity_case_count``.
    """
    if not iri:
        return [], []
    try:
        from cases.models import CaseEntityRelationship, CaseState

        case_ids = set(
            CaseEntityRelationship.objects.filter(
                case__state=CaseState.PUBLISHED, nes_id=iri
            ).values_list("case_id", flat=True)
        )
        if not case_ids:
            return [], []
        places = place_index()
        target: tuple[set[str], set[str]] = (set(), set())
        for _case_id, place_iri in _location_binds_for_cases(case_ids):
            _merge(target, places.get(place_iri))
        return _sorted(target)
    except Exception:  # noqa: BLE001 — best-effort; the reindex repairs it.
        logger.warning(
            "entity_locations failed for %s; indexing with no geography",
            iri,
            exc_info=True,
        )
        return [], []


def _location_binds_for_cases(case_ids: Iterable[int]) -> list[tuple[int, str]]:
    """``(case id, location-bind IRI)`` pairs for the given cases (any state).

    The caller has already restricted ``case_ids`` to published cases, so this
    does not re-filter on state. Pairs rather than a flat set because
    :func:`locations_for_iris` has to keep each case's places separate — two
    entities on the batch may be cited by different subsets of the cases.
    """
    from cases.models import CaseEntityRelationship, RelationshipType

    return [
        (case_id, nes_id)
        for case_id, nes_id in CaseEntityRelationship.objects.filter(
            case_id__in=list(case_ids), relationship_type=RelationshipType.LOCATION
        ).values_list("case_id", "nes_id")
        if nes_id
    ]


def case_geography(case: Any) -> tuple[list[str], list[str]]:
    """``(districts, provinces)`` for ONE case, from its own location binds.

    Used by the case indexer. Unlike the entity path this needs no state
    filter: ``cases.search_index`` only ever indexes a PUBLISHED case and
    deletes the document for every other state.

    Best-effort — a case must still index if the place lookup fails, with the
    geography missing rather than the document.
    """
    from cases.models import RelationshipType

    try:
        relationships = getattr(case, "entity_relationships", None)
        if relationships is None:
            return [], []
        # ``.all()`` + a Python filter, not ``.filter(relationship_type=...)``:
        # a queryset filter bypasses ``prefetch_related``'s cache and would
        # issue a second query per case on the paths that prefetch. The bind
        # list is human-scale (26 at the prod maximum).
        place_iris = [
            rel.nes_id
            for rel in relationships.all()
            if rel.nes_id and rel.relationship_type == RelationshipType.LOCATION
        ]
        if not place_iris:
            return [], []
        places = place_index()
        target: tuple[set[str], set[str]] = (set(), set())
        for place_iri in place_iris:
            _merge(target, places.get(place_iri))
        return _sorted(target)
    except Exception:  # noqa: BLE001 — best-effort, same as the live entity path.
        logger.warning(
            "case_geography failed for %s; indexing with no geography",
            getattr(case, "pk", None),
            exc_info=True,
        )
        return [], []


def clear_cache() -> None:
    """Invalidate the place graph.

    Called when a place entity is written. Case and bind writes do NOT need it:
    they change which places a record points AT, not what a place resolves to.
    """
    cache.delete(PLACES_CACHE_KEY)
