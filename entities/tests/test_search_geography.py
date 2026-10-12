"""Subject geography: the ``district``/``province`` facet values.

Three things are under test, in rising order of how expensive getting them
wrong would be:

1. the VOCABULARY (``PCODE_DISTRICT`` / ``PCODE_PROVINCE``) — it has to be the
   same 77 districts and 7 provinces ``courts.geography`` already publishes, or
   a district arrives in the facet under two spellings and splits its bucket;
2. the PLACE GRAPH (``_build_place_index``) — pure shaping, and the place a
   wrong answer is cheapest to produce: an unresolvable location must stay
   unresolved rather than be guessed into a plausible district;
3. the DERIVATION (``case_locations`` / ``entity_locations`` /
   ``case_geography``) and its triggers, which is where the published-only rule
   and the fan-out live.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipType,
)
from courts.geography import DISTRICT_PROVINCE
from entities.models import StoredEntity
from entities.search_geography import (
    GeographyLookupError,
    MAX_PLACE_DEPTH,
    PCODE_DISTRICT,
    PCODE_PROVINCE,
    _build_place_index,
    case_geography,
    case_locations,
    clear_cache,
    entity_locations,
    locations_for_iris,
    place_index,
)

IRI_BASE = "https://jawafdehi.org/entity"
PERSON = f"{IRI_BASE}/person/geo-test-subject"
OTHER = f"{IRI_BASE}/person/geo-test-bystander"
KATHMANDU = f"{IRI_BASE}/location/district/kathmandu-np0327"
RUPANDEHI = f"{IRI_BASE}/location/district/rupandehi-np0548"
KMC = f"{IRI_BASE}/location/localunit/kathmandu-metropolitian-city-30608"
BAGMATI = f"{IRI_BASE}/location/province/bagmati-np03"
ADHOC = f"{IRI_BASE}/location/jamal-kathmandu"


def _place(iri: str, *, pcode: str | None = None, parent: str | None = None) -> dict:
    data: dict = {"@id": iri, "@type": "AdministrativeArea"}
    if pcode:
        data["identifier"] = [
            {"@type": "PropertyValue", "propertyID": "ocha-pcode", "value": pcode}
        ]
    if parent:
        data["containedInPlace"] = {"@id": parent}
    return data


def _index(*records: dict) -> dict:
    return _build_place_index((r["@id"], r) for r in records)


def test_district_table_is_a_bijection_with_the_court_vocabulary():
    """Every p-code names a district ``courts.geography`` knows, and every
    district ``courts.geography`` knows has a p-code.

    This is the pin that keeps one district out of two facet buckets. A missing
    entry silently drops a district's entities out of the facet; an extra one
    introduces a spelling the court documents never use."""
    assert len(PCODE_DISTRICT) == 77
    assert set(PCODE_DISTRICT.values()) == set(DISTRICT_PROVINCE)
    assert len(set(PCODE_DISTRICT.values())) == len(PCODE_DISTRICT)


def test_province_table_matches_the_court_vocabulary():
    assert set(PCODE_PROVINCE.values()) == set(DISTRICT_PROVINCE.values())


def test_the_split_districts_follow_the_pcode_not_the_name():
    """The reason this table is keyed on p-codes at all.

    The entity store calls them ``Nawalparasi East``/``Nawalparasi West`` and
    ``Rukum East``/``Rukum West``; ``courts.geography`` calls the same four
    ``Nawalpur``/``Nawalparasi`` and ``Rukumkot``/``Rukum``. Normalizing by name
    would map ``Nawalparasi East`` onto ``Nawalparasi`` — which is the WEST one
    — and file every Gandaki case under a Lumbini district. These four
    assertions are the regression guard for exactly that."""
    assert PCODE_DISTRICT["NP0447"] == "Nawalpur"  # OCHA "Nawalparasi East"
    assert PCODE_DISTRICT["NP0547"] == "Nawalparasi"  # OCHA "Nawalparasi West"
    assert PCODE_DISTRICT["NP0552"] == "Rukumkot"  # OCHA "Rukum East"
    assert PCODE_DISTRICT["NP0652"] == "Rukum"  # OCHA "Rukum West"
    assert DISTRICT_PROVINCE["Nawalpur"] == "Gandaki"
    assert DISTRICT_PROVINCE["Nawalparasi"] == "Lumbini"


def test_a_district_resolves_to_itself_and_its_province():
    index = _index(_place(KATHMANDU, pcode="NP0327"))
    assert index[KATHMANDU] == ["Kathmandu", "Bagmati"]


def test_a_local_unit_inherits_its_district():
    index = _index(
        _place(KATHMANDU, pcode="NP0327"),
        _place(KMC, pcode="NP30608", parent=KATHMANDU),
    )
    assert index[KMC] == ["Kathmandu", "Bagmati"]


def test_a_province_resolves_to_a_province_with_no_district():
    """A case that names only a province is about the province, and inventing a
    district for it would be a fabricated bucket."""
    index = _index(_place(BAGMATI, pcode="NP03"))
    assert index[BAGMATI] == [None, "Bagmati"]


def test_an_adhoc_place_resolves_to_nothing():
    """``location/jamal-kathmandu`` is free text the enricher minted. The slug
    contains "kathmandu" and the temptation is to read it; the cost of being
    wrong is a case filed in a district it has nothing to do with."""
    assert _index(_place(ADHOC)) == {}


def test_a_place_under_an_unresolvable_parent_resolves_to_nothing():
    index = _index(_place(ADHOC), _place(KMC, parent=ADHOC))
    assert index == {}


def test_an_unknown_pcode_resolves_to_nothing():
    """A foreign place (the store holds a handful) carries no Nepali p-code."""
    assert _index(_place(f"{IRI_BASE}/location/the-hague", pcode="NL0518")) == {}


def test_a_containment_cycle_terminates():
    a = f"{IRI_BASE}/location/a"
    b = f"{IRI_BASE}/location/b"
    assert _index(_place(a, parent=b), _place(b, parent=a)) == {}


def test_containment_deeper_than_the_bound_is_not_followed():
    """The real graph is three levels. The bound exists so a pathological chain
    cannot make a reindex walk the whole store per document."""
    chain = [f"{IRI_BASE}/location/step-{i}" for i in range(MAX_PLACE_DEPTH + 3)]
    records = [_place(KATHMANDU, pcode="NP0327")]
    records += [
        _place(iri, parent=(chain[i + 1] if i + 1 < len(chain) else KATHMANDU))
        for i, iri in enumerate(chain)
    ]
    index = _index(*records)
    assert index[chain[-1]] == ["Kathmandu", "Bagmati"]
    assert chain[0] not in index


def test_pcode_is_read_from_the_identifier_and_case_folded():
    data = _place(KATHMANDU)
    data["identifier"] = [
        {"propertyID": "cbs-district-code", "value": "999"},
        {"propertyID": "ocha-pcode", "value": " np0327 "},
    ]
    assert _build_place_index([(KATHMANDU, data)])[KATHMANDU][0] == "Kathmandu"


def test_contained_in_place_accepts_a_bare_iri_string():
    data = _place(KMC)
    data["containedInPlace"] = KATHMANDU
    index = _build_place_index(
        [(KATHMANDU, _place(KATHMANDU, pcode="NP0327")), (KMC, data)]
    )
    assert index[KMC] == ["Kathmandu", "Bagmati"]


def _entity(iri: str, data: dict | None = None) -> StoredEntity:
    prefix, _, slug = iri.removeprefix(f"{IRI_BASE}/").rpartition("/")
    return StoredEntity.objects.create(
        iri=iri,
        entity_type="Person",
        prefix=prefix,
        slug=slug,
        data=data or {"@id": iri, "@type": "Person", "name": {"en": slug}},
    )


def _case(state: str = CaseState.PUBLISHED, title: str = "Geo test") -> Case:
    return Case.objects.create(
        title=title, offence_type=CaseType.CORRUPTION, state=state
    )


def _bind(case: Case, iri: str, role: str = RelationshipType.ACCUSED):
    return CaseEntityRelationship.objects.create(
        case=case, nes_id=iri, relationship_type=role
    )


class DerivationTests(TestCase):
    """``case_locations`` / ``entity_locations`` / ``case_geography``."""

    databases = "__all__"

    def setUp(self):
        cache.clear()
        for target in (
            "entities.search_index.index",
            "entities.search_index.delete",
            "entities.search_index.index_by_iri",
            "cases.search_index.index",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        _entity(KATHMANDU, _place(KATHMANDU, pcode="NP0327"))
        _entity(RUPANDEHI, _place(RUPANDEHI, pcode="NP0548"))
        _entity(KMC, _place(KMC, pcode="NP30608", parent=KATHMANDU))
        _entity(ADHOC, _place(ADHOC))
        clear_cache()

    def test_an_entity_takes_the_district_of_the_case_citing_it(self):
        _entity(PERSON)
        case = _case()
        _bind(case, PERSON)
        _bind(case, KATHMANDU, RelationshipType.LOCATION)
        assert entity_locations(PERSON) == (["Kathmandu"], ["Bagmati"])
        assert case_locations()[PERSON] == (["Kathmandu"], ["Bagmati"])

    def test_a_local_unit_bind_resolves_up_to_its_district(self):
        _entity(PERSON)
        case = _case()
        _bind(case, PERSON)
        _bind(case, KMC, RelationshipType.LOCATION)
        assert entity_locations(PERSON) == (["Kathmandu"], ["Bagmati"])

    def test_two_cases_in_two_districts_give_the_entity_both(self):
        """Multi-valued on purpose: picking one would be a lie about the other."""
        _entity(PERSON)
        first, second = _case(), _case(title="Geo test 2")
        _bind(first, PERSON)
        _bind(first, KATHMANDU, RelationshipType.LOCATION)
        _bind(second, PERSON)
        _bind(second, RUPANDEHI, RelationshipType.LOCATION)
        districts, provinces = entity_locations(PERSON)
        assert districts == ["Kathmandu", "Rupandehi"]
        assert provinces == ["Bagmati", "Lumbini"]

    def test_values_are_sorted_so_the_document_is_byte_stable(self):
        """An unstable field would make every reindex rewrite every document."""
        _entity(PERSON)
        case = _case()
        _bind(case, PERSON)
        _bind(case, RUPANDEHI, RelationshipType.LOCATION)
        _bind(case, KATHMANDU, RelationshipType.LOCATION)
        assert entity_locations(PERSON)[0] == ["Kathmandu", "Rupandehi"]

    def test_a_draft_case_contributes_no_geography(self):
        """Mirrors the visibility gate: an unpublished case's location is not
        public information."""
        _entity(PERSON)
        case = _case(state=CaseState.DRAFT)
        _bind(case, PERSON)
        _bind(case, KATHMANDU, RelationshipType.LOCATION)
        assert entity_locations(PERSON) == ([], [])
        assert case_locations() == {}

    def test_an_adhoc_location_bind_contributes_nothing(self):
        _entity(PERSON)
        case = _case()
        _bind(case, PERSON)
        _bind(case, ADHOC, RelationshipType.LOCATION)
        assert entity_locations(PERSON) == ([], [])

    def test_a_location_bind_pointing_at_an_organization_is_ignored(self):
        """A real data shape: 3 of 438 bound "locations" in prod are
        organizations or people. It must cost the facet a value, not raise."""
        org = f"{IRI_BASE}/organization/government"
        _entity(org)
        _entity(PERSON)
        case = _case()
        _bind(case, PERSON)
        _bind(case, org, RelationshipType.LOCATION)
        assert entity_locations(PERSON) == ([], [])

    def test_a_case_with_no_location_bind_gives_its_entities_none(self):
        _entity(PERSON)
        _bind(_case(), PERSON)
        assert entity_locations(PERSON) == ([], [])

    def test_an_uncited_entity_has_no_geography(self):
        _entity(PERSON)
        assert entity_locations(PERSON) == ([], [])
        assert PERSON not in case_locations()

    def test_only_the_cited_entities_of_the_located_case_are_affected(self):
        """The geography flows along the case, not across the corpus."""
        _entity(PERSON)
        _entity(OTHER)
        case = _case()
        _bind(case, PERSON)
        _bind(case, KATHMANDU, RelationshipType.LOCATION)
        _bind(_case(title="elsewhere"), OTHER)
        assert entity_locations(OTHER) == ([], [])

    def test_case_geography_reads_the_cases_own_binds(self):
        case = _case()
        _bind(case, KMC, RelationshipType.LOCATION)
        assert case_geography(case) == (["Kathmandu"], ["Bagmati"])

    def test_case_geography_of_a_case_with_no_binds_is_empty(self):
        assert case_geography(_case()) == ([], [])

    def test_the_place_entity_itself_carries_its_own_district(self):
        """The bound district is also a cited entity, so it gets the facet value
        too — which is what makes "Kathmandu" findable under its own filter."""
        case = _case()
        _bind(case, KATHMANDU, RelationshipType.LOCATION)
        assert case_locations()[KATHMANDU] == (["Kathmandu"], ["Bagmati"])

    def test_the_batch_form_answers_each_entity_separately(self):
        """``locations_for_iris`` is what the fan-out uses, and the whole point
        is that two entities in the batch can sit in different districts — a
        shared answer would file them all under the first case's place."""
        _entity(PERSON)
        _entity(OTHER)
        here, there = _case(), _case(title="elsewhere")
        _bind(here, PERSON)
        _bind(here, KATHMANDU, RelationshipType.LOCATION)
        _bind(there, OTHER)
        _bind(there, RUPANDEHI, RelationshipType.LOCATION)
        batch = locations_for_iris([PERSON, OTHER])
        assert batch[PERSON] == (["Kathmandu"], ["Bagmati"])
        assert batch[OTHER] == (["Rupandehi"], ["Lumbini"])

    def test_the_batch_form_agrees_with_the_single_entity_form(self):
        _entity(PERSON)
        first, second = _case(), _case(title="second")
        _bind(first, PERSON)
        _bind(first, KATHMANDU, RelationshipType.LOCATION)
        _bind(second, PERSON)
        _bind(second, RUPANDEHI, RelationshipType.LOCATION)
        assert locations_for_iris([PERSON])[PERSON] == entity_locations(PERSON)

    def test_the_batch_form_omits_an_uncited_entity(self):
        """Absent means "no published citation, so no geography" — the caller
        renders it as empty rather than as unknown."""
        _entity(PERSON)
        assert locations_for_iris([PERSON, OTHER]) == {}

    def test_the_batch_form_issues_one_resolution_for_the_whole_list(self):
        """The reason it exists: a 26-entity fan-out must not be 26 lookups."""
        iris = []
        case = _case()
        _bind(case, KATHMANDU, RelationshipType.LOCATION)
        for n in range(8):
            iri = f"{IRI_BASE}/person/batch-{n}"
            _entity(iri)
            _bind(case, iri)
            iris.append(iri)
        place_index()  # warm the cached place graph, as the real path does
        with CaptureQueriesContext(connection) as ctx:
            batch = locations_for_iris(iris)
        assert all(batch[iri] == (["Kathmandu"], ["Bagmati"]) for iri in iris)
        assert len(ctx) <= 3, [q["sql"] for q in ctx]

    def test_bulk_lookup_raises_rather_than_reporting_an_empty_corpus(self):
        """The blast-radius split. An empty map here would strip the facet off
        every document in one alias swap, so the reindex must abort instead."""
        with patch(
            "entities.search_geography.place_index", side_effect=RuntimeError("down")
        ):
            with pytest.raises(GeographyLookupError):
                case_locations()

    def test_the_live_path_degrades_to_empty_instead_of_raising(self):
        """One document indexed without geography is repaired by the next
        reindex; an exception here would abort the whole entity save's
        indexing."""
        _entity(PERSON)
        case = _case()
        _bind(case, PERSON)
        _bind(case, KATHMANDU, RelationshipType.LOCATION)
        with patch(
            "entities.search_geography.place_index", side_effect=RuntimeError("down")
        ):
            assert entity_locations(PERSON) == ([], [])

    def test_case_geography_degrades_to_empty_instead_of_raising(self):
        case = _case()
        _bind(case, KATHMANDU, RelationshipType.LOCATION)
        with patch(
            "entities.search_geography.place_index", side_effect=RuntimeError("down")
        ):
            assert case_geography(case) == ([], [])

    def test_the_place_graph_is_cached_and_invalidated_by_a_place_write(self):
        """The cache is what keeps a 204k-document reindex from re-reading 838
        place rows per document; the invalidation is what keeps a corrected
        ``containedInPlace`` from taking an hour to land."""
        assert place_index()[KMC] == ["Kathmandu", "Bagmati"]
        StoredEntity.objects.filter(iri=KMC).update(
            data=_place(KMC, pcode="NP30608", parent=RUPANDEHI)
        )
        assert place_index()[KMC] == ["Kathmandu", "Bagmati"], "still cached"
        clear_cache()
        assert place_index()[KMC] == ["Rupandehi", "Lumbini"]


class GeographyTriggerTests(TestCase):
    """``cases.signals``: a location change has to reach the OTHER entities.

    Every other bind role changes one entity's own derived values, so the
    existing per-row trigger is enough. A LOCATION bind is different: it says
    where the case happened, which is the district of every entity the case
    cites. Without a fan-out, moving a case from Kathmandu to Rupandehi
    re-indexes the two place entities and leaves the accused filed under the old
    district until the weekly reindex.
    """

    databases = "__all__"

    def setUp(self):
        cache.clear()
        for target in (
            "entities.search_index.index",
            "entities.search_index.delete",
            "cases.search_index.index",
        ):
            patcher = patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        reindex_patcher = patch("entities.search_index.index_by_iri")
        self.reindex = reindex_patcher.start()
        self.addCleanup(reindex_patcher.stop)
        _entity(KATHMANDU, _place(KATHMANDU, pcode="NP0327"))
        _entity(RUPANDEHI, _place(RUPANDEHI, pcode="NP0548"))
        clear_cache()

    def _on_commit(self, write):
        """Run ``write`` and then its ``on_commit`` callbacks.

        ``TestCase`` wraps each test in a transaction that never commits, so
        without this the deferred re-indexes are collected and discarded and
        every assertion below would pass vacuously."""
        with self.captureOnCommitCallbacks(execute=True):
            return write()

    def _locations_written(self) -> dict:
        """``{iri: last (districts, provinces) written}`` across every call."""
        written = {}
        for call in self.reindex.call_args_list:
            iri = call.args[0] if call.args else call.kwargs["iri"]
            written[iri] = call.kwargs.get("locations")
        return written

    def test_a_location_bind_fans_out_to_the_cases_other_entities(self):
        _entity(PERSON)
        case = _case()
        self._on_commit(lambda: _bind(case, PERSON))
        self.reindex.reset_mock()
        self._on_commit(lambda: _bind(case, KATHMANDU, RelationshipType.LOCATION))
        written = self._locations_written()
        assert written[PERSON] == (["Kathmandu"], ["Bagmati"])

    def test_removing_a_location_bind_clears_the_other_entities(self):
        _entity(PERSON)
        case = _case()
        _bind(case, PERSON)
        location = _bind(case, KATHMANDU, RelationshipType.LOCATION)
        self.reindex.reset_mock()
        self._on_commit(location.delete)
        assert self._locations_written()[PERSON] == ([], [])

    def test_a_non_location_bind_does_not_fan_out(self):
        """The fan-out is scoped to the one role that changes other documents.
        Widening it would turn every accused bind on a 20-entity case into 20
        re-indexes."""
        _entity(PERSON)
        _entity(OTHER)
        case = _case()
        _bind(case, OTHER)
        self.reindex.reset_mock()
        self._on_commit(lambda: _bind(case, PERSON))
        assert set(self._locations_written()) == {PERSON}
