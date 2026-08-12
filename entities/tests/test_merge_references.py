"""Reference counting and repointing across the default, ngm and nes databases."""

from datetime import date

import pytest

from cases.models import Case, CaseEntityRelationship, CaseState, CaseType, RelationshipOutcome, RelationshipType
from courts.models import BlacklistedFirm, CaseEntity, Court, CourtCase
from entities.models import StoredEntity
from entities.services.merge import references

JHAPA = "https://jawafdehi.org/entity/location/district/jhapa-np0104"
LOOSE = "https://jawafdehi.org/entity/location/jhapa"

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
    counts = references.count_references([LOOSE], JHAPA)
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


def test_a_deduplicated_bind_is_recoverable_from_the_manifest():
    case = _case("jhapa-revenue-9")
    CaseEntityRelationship.objects.create(
        case=case, nes_id=JHAPA, relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.CHARGED,
    )
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.CONVICTED, notes="from duplicate",
    )
    _, manifest = references.repoint_case_binds([LOOSE], JHAPA)
    entry = next(e for e in manifest if e["action"] == "deduplicated")
    assert entry["deleted_row"]["outcome"] == RelationshipOutcome.CONVICTED
    assert entry["deleted_row"]["notes"] == "from duplicate"


def test_two_duplicates_on_one_case_fold_into_a_single_bind():
    # The survivor has no bind: the first duplicate repoints, the second folds into it.
    case = _case("jhapa-revenue-7")
    other = "https://jawafdehi.org/entity/location/jhapa-district"
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.LOCATION
    )
    CaseEntityRelationship.objects.create(
        case=case, nes_id=other, relationship_type=RelationshipType.LOCATION
    )
    counts, _ = references.repoint_case_binds([LOOSE, other], JHAPA)
    assert counts.repointed == 1
    assert counts.deduplicated == 1
    assert CaseEntityRelationship.objects.filter(case=case).count() == 1


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


def test_a_conflict_reports_its_relationship_type_alongside_the_case_id():
    case = _case("jhapa-revenue-8")
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
    assert conflicts[0]["relationship_type"] == RelationshipType.ACCUSED


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


def test_two_duplicates_disagreeing_on_a_verdict_are_reported_as_a_conflict():
    # The survivor has no bind at all — the disagreement is between the duplicates.
    case = _case("jhapa-revenue-6")
    other = "https://jawafdehi.org/entity/location/jhapa-district"
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.CONVICTED,
    )
    CaseEntityRelationship.objects.create(
        case=case, nes_id=other, relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.ACQUITTED,
    )
    conflicts = references.detect_outcome_conflicts([LOOSE, other], JHAPA)
    assert len(conflicts) == 1
    assert conflicts[0]["case_id"] == case.id
    assert set(conflicts[0]["outcomes"].values()) == {
        RelationshipOutcome.CONVICTED, RelationshipOutcome.ACQUITTED
    }


def test_the_link_count_matches_what_the_repoint_reports():
    _entity(JHAPA)
    damak = "https://jawafdehi.org/entity/location/localunit/damak-10502"
    _entity(damak, data={
        "@id": damak, "@type": "Place", "name": {"en": "Damak"},
        "containedInPlace": {"@id": LOOSE},
        "jawafdehi:supersedes": {"@id": LOOSE},
    })
    counted = references.count_references([LOOSE], JHAPA)["entity_to_entity_links"]
    moved, _ = references.repoint_entity_links(
        [LOOSE], JHAPA, author_id="oidc:seed", merge_id="test-merge"
    )
    assert counted == 2
    assert moved.repointed == counted


def test_counts_reach_zero_once_the_links_are_repointed():
    # What makes the endpoint able to answer "already_merged" on a repeat request.
    _entity(JHAPA, data={"@id": JHAPA, "@type": "Place", "name": {"en": "Jhapa"},
                         "sameAs": [LOOSE]})
    damak = "https://jawafdehi.org/entity/location/localunit/damak-10502"
    _entity(damak, data={
        "@id": damak, "@type": "Place", "name": {"en": "Damak"},
        "containedInPlace": {"@id": LOOSE},
    })
    references.repoint_entity_links(
        [LOOSE], JHAPA, author_id="oidc:seed", merge_id="test-merge"
    )
    assert references.count_references([LOOSE], JHAPA)["entity_to_entity_links"] == 0


def test_the_survivors_own_document_is_never_rewritten():
    _entity(JHAPA, data={
        "@id": JHAPA, "@type": "Place", "name": {"en": "Jhapa"},
        "jawafdehi:supersedes": {"@id": LOOSE},
    })
    counts, manifest = references.repoint_entity_links(
        [LOOSE], JHAPA, author_id="oidc:seed", merge_id="test-merge"
    )
    assert counts.repointed == 0
    assert manifest == []
    assert StoredEntity.objects.get(pk=JHAPA).data["jawafdehi:supersedes"]["@id"] == LOOSE
