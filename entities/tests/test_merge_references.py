"""Reference counting and repointing across the default, ngm and nes databases."""

from datetime import date

import pytest

from cases.models import Case, CaseEntityRelationship, CaseState, CaseType, RelationshipOutcome, RelationshipType
from courts.models import BlacklistedFirm, CaseEntity, Court, CourtCase
from entities.models import StoredEntity
from entities.services.merge import references

JHAPA = "https://jawafdehi.org/entity/location/district/jhapa-np0104"
LOOSE = "https://jawafdehi.org/entity/location/jhapa"
RAM = "https://jawafdehi.org/entity/person/ram-bahadur"

pytestmark = pytest.mark.django_db(databases="__all__")


def _case(slug):
    return Case.objects.create(
        title="Jhapa land revenue case", slug=slug, case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT, short_description="test", description="test",
    )


def _entity(iri, data=None):
    return StoredEntity.objects.create(
        iri=iri, entity_type="Place", prefix="location", slug=iri.rsplit("/", 1)[-1],
        data=data or {"@id": iri, "@type": "Place", "name": {"en": "Jhapa"}},
    )


def test_count_reports_every_store_key_even_when_zero():
    counts = references.count_references([LOOSE])
    assert set(counts) == set(references.STORE_KEYS)
    assert all(v == 0 for v in counts.values())


def test_case_bind_is_repointed_to_the_survivor():
    case = _case("jhapa-revenue-1")
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.LOCATION
    )
    counts, manifest = references.repoint_case_binds([LOOSE], JHAPA)
    assert counts.repointed == 1
    assert CaseEntityRelationship.objects.filter(nes_id=JHAPA).count() == 1
    assert not CaseEntityRelationship.objects.filter(nes_id=LOOSE).exists()
    assert manifest[0]["action"] == "repointed"


def test_colliding_bind_folds_into_its_sibling_instead_of_duplicating():
    case = _case("jhapa-revenue-2")
    CaseEntityRelationship.objects.create(
        case=case, nes_id=JHAPA, relationship_type=RelationshipType.LOCATION, notes="from survivor"
    )
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.LOCATION, notes="from duplicate"
    )
    counts, _ = references.repoint_case_binds([LOOSE], JHAPA)
    assert counts.deduplicated == 1
    assert counts.repointed == 0
    rows = CaseEntityRelationship.objects.filter(case=case)
    assert rows.count() == 1
    assert "from duplicate" in rows.first().notes


def test_terminal_verdict_beats_charged_when_binds_collide():
    case = _case("jhapa-revenue-3")
    CaseEntityRelationship.objects.create(
        case=case, nes_id=JHAPA, relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.CHARGED,
    )
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.CONVICTED,
    )
    references.repoint_case_binds([LOOSE], JHAPA)
    row = CaseEntityRelationship.objects.get(case=case)
    assert row.outcome == RelationshipOutcome.CONVICTED


def test_two_different_terminal_verdicts_are_reported_as_a_conflict():
    case = _case("jhapa-revenue-4")
    CaseEntityRelationship.objects.create(
        case=case, nes_id=JHAPA, relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.ACQUITTED,
    )
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.CONVICTED,
    )
    conflicts = references.detect_outcome_conflicts([LOOSE], JHAPA)
    assert len(conflicts) == 1
    assert conflicts[0]["case_id"] == case.id


def test_all_three_ngm_stores_are_repointed():
    court = Court.objects.create(
        identifier="jhapadc", court_type="district",
        full_name_nepali="जिल्ला अदालत झापा", full_name_english="District Court Jhapa",
    )
    CourtCase.objects.create(case_number="081-CR-0081", court=court, nes_id=LOOSE)
    CaseEntity.objects.create(
        case_number="081-CR-0081", court=court, side="defendant",
        name="Ram Bahadur", nes_id=LOOSE,
    )
    BlacklistedFirm.objects.create(
        firm_name="Damak Nirman Sewa", blacklist_date_bs="2080-01-01",
        blacklist_date_ad=date(2023, 4, 14), nes_id=LOOSE,
    )
    per_store, manifest = references.repoint_court_rows([LOOSE], JHAPA)
    assert per_store["court_cases"].repointed == 1
    assert per_store["court_case_parties"].repointed == 1
    assert per_store["blacklisted_firms"].repointed == 1
    assert not CourtCase.objects.filter(nes_id=LOOSE).exists()
    assert not CaseEntity.objects.filter(nes_id=LOOSE).exists()
    assert not BlacklistedFirm.objects.filter(nes_id=LOOSE).exists()
    # CourtCase has a composite PK — the manifest records court/case_number.
    assert any(e["pk"] == "jhapadc/081-CR-0081" for e in manifest)


def test_entity_to_entity_link_is_repointed_and_versioned():
    _entity(JHAPA)
    damak = "https://jawafdehi.org/entity/location/localunit/damak-10502"
    _entity(damak, data={
        "@id": damak, "@type": "Place", "name": {"en": "Damak"},
        "containedInPlace": {"@id": LOOSE},
    })
    counts, manifest = references.repoint_entity_links(
        [LOOSE], JHAPA, author_id="oidc:seed", merge_id="test-merge"
    )
    assert counts.repointed == 1
    row = StoredEntity.objects.get(pk=damak)
    assert row.data["containedInPlace"]["@id"] == JHAPA
    assert row.version == 2
    assert manifest[0]["store"] == "entity_to_entity_links"


def test_a_tombstoned_entity_is_not_scanned_for_links():
    _entity(JHAPA)
    stale = "https://jawafdehi.org/entity/location/stale-place"
    StoredEntity.objects.create(
        iri=stale, entity_type="Place", prefix="location", slug="stale-place",
        data={"@id": stale, "@type": "Place", "name": {"en": "Stale"},
              "containedInPlace": {"@id": LOOSE}},
        is_deleted=True,
    )
    counts, _ = references.repoint_entity_links(
        [LOOSE], JHAPA, author_id="oidc:seed", merge_id="test-merge"
    )
    assert counts.repointed == 0


def test_repointing_twice_is_a_noop_the_second_time():
    case = _case("jhapa-revenue-5")
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.LOCATION
    )
    references.repoint_case_binds([LOOSE], JHAPA)
    counts, manifest = references.repoint_case_binds([LOOSE], JHAPA)
    assert counts.repointed == 0
    assert manifest == []
