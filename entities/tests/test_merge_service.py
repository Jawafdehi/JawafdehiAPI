"""EntityMergeService — phases, rejections, idempotency, resume."""

import json

import pytest

from cases.models import (
    Case, CaseEntityRelationship, CaseState, CaseType, RelationshipOutcome, RelationshipType,
)
from courts.models import Court, CourtCase
from entities.models import EntityMerge, StoredEntity
from entities.services.merge import EntityMergeService, MergeError
from entities.services.publication import PublicationService
from entities.write_validation import normalize_authoring_payload

JHAPA = "https://jawafdehi.org/entity/location/district/jhapa-np0104"
LOOSE = "https://jawafdehi.org/entity/location/jhapa"

pytestmark = pytest.mark.django_db(databases="__all__")


def _seed(prefix, slug, atype, **props):
    payload = {"prefix": prefix, "slug": slug, "type": atype,
               "name": {"en": "Jhapa", "ne": "झापा"}, **props}
    return PublicationService().create_entity(
        doc=normalize_authoring_payload(payload), author_id="oidc:seed",
        change_description="seed",
    )


def _seed_pair():
    _seed("location/district", "jhapa-np0104", ["AdministrativeArea", "jawafdehi:District"],
          identifier=[{"@type": "PropertyValue", "propertyID": "ocha-pcode", "value": "NP0104"}])
    _seed("location", "jhapa", "Place", description={"ne": "झापा जिल्ला"})


def _merge(**kwargs):
    kwargs.setdefault("survivor_iri", JHAPA)
    kwargs.setdefault("duplicate_iris", [LOOSE])
    kwargs.setdefault("author_id", "oidc:377592055028777324")
    return EntityMergeService().merge(**kwargs)


def _crash_once_in_court_rows(**kwargs):
    """Run a merge that dies in the ngm phase; return the PENDING merge's id."""
    from entities.services.merge import service as svc

    original = svc.references.repoint_court_rows

    def _boom(*args, **kwargs):
        raise RuntimeError("ngm unreachable")

    svc.references.repoint_court_rows = _boom
    try:
        with pytest.raises(MergeError) as exc:
            _merge(**kwargs)
        assert exc.value.code == "MERGE_INCOMPLETE"
        assert exc.value.http_status == 500
        return exc.value.extra["merge_id"]
    finally:
        svc.references.repoint_court_rows = original


def test_happy_path_tombstones_the_duplicate_and_keeps_the_survivor():
    _seed_pair()
    result = _merge()
    assert result["status"] == "complete"
    dup = StoredEntity.objects.get(pk=LOOSE)
    assert dup.is_deleted is True
    assert dup.merged_into == JHAPA
    survivor = StoredEntity.objects.get(pk=JHAPA)
    assert survivor.is_deleted is False
    assert survivor.version == 2
    assert survivor.data["description"] == {"ne": "झापा जिल्ला"}
    assert LOOSE in survivor.data["sameAs"]


def test_merge_record_captures_snapshots_and_completes():
    _seed_pair()
    result = _merge()
    merge = EntityMerge.objects.get(pk=result["merge_id"])
    assert merge.status == EntityMerge.COMPLETE
    assert merge.completed_at is not None
    assert merge.duplicate_snapshots[LOOSE]["@type"] == "Place"
    assert merge.survivor_snapshot_before["@id"] == JHAPA


def test_dry_run_writes_nothing():
    _seed_pair()
    result = _merge(dry_run=True)
    assert result["status"] == "planned"
    assert result["merge_id"] is None
    assert StoredEntity.objects.get(pk=LOOSE).is_deleted is False
    assert StoredEntity.objects.get(pk=JHAPA).version == 1
    assert not EntityMerge.objects.exists()


def test_rerunning_the_same_merge_is_a_safe_noop():
    _seed_pair()
    _merge()
    result = _merge()
    assert result["status"] == "already_merged"
    assert result["merge_id"] is None
    assert result["total_references"] == 0
    assert StoredEntity.objects.get(pk=JHAPA).version == 2
    assert EntityMerge.objects.count() == 1


def test_self_merge_is_rejected():
    _seed_pair()
    with pytest.raises(MergeError) as exc:
        _merge(duplicate_iris=[JHAPA])
    assert exc.value.code == "SELF_MERGE"
    assert exc.value.http_status == 422


def test_cross_family_merge_is_rejected():
    _seed("location/district", "jhapa-np0104", "AdministrativeArea")
    _seed("person", "ram-bahadur", "Person")
    with pytest.raises(MergeError) as exc:
        _merge(duplicate_iris=["https://jawafdehi.org/entity/person/ram-bahadur"])
    assert exc.value.code == "TYPE_MISMATCH"
    assert exc.value.http_status == 422
    assert StoredEntity.objects.get(pk="https://jawafdehi.org/entity/person/ram-bahadur").is_deleted is False


def test_declared_type_family_must_agree_with_the_survivor():
    _seed_pair()
    with pytest.raises(MergeError) as exc:
        _merge(type_family="person")
    assert exc.value.code == "TYPE_MISMATCH"
    assert exc.value.http_status == 422


def test_unknown_duplicate_is_a_not_found():
    _seed("location/district", "jhapa-np0104", "AdministrativeArea")
    with pytest.raises(MergeError) as exc:
        _merge(duplicate_iris=["https://jawafdehi.org/entity/location/nowhere"])
    assert exc.value.code == "NOT_FOUND"
    assert exc.value.http_status == 404


def test_merging_into_a_retired_survivor_is_rejected():
    _seed_pair()
    _seed("location", "jhapa-older", "Place")
    _merge()
    with pytest.raises(MergeError) as exc:
        _merge(survivor_iri=LOOSE,
               duplicate_iris=["https://jawafdehi.org/entity/location/jhapa-older"])
    assert exc.value.code == "SURVIVOR_RETIRED"
    assert exc.value.http_status == 409
    assert exc.value.extra["merged_into"] == JHAPA


def test_duplicate_already_merged_elsewhere_is_a_conflict():
    _seed_pair()
    _seed("location/district", "kaski-np0439", "AdministrativeArea")
    _merge()
    with pytest.raises(MergeError) as exc:
        _merge(survivor_iri="https://jawafdehi.org/entity/location/district/kaski-np0439")
    assert exc.value.code == "DUPLICATE_ALREADY_MERGED"
    assert exc.value.http_status == 409


def test_too_many_duplicates_is_rejected():
    _seed_pair()
    with pytest.raises(MergeError) as exc:
        _merge(duplicate_iris=[f"{LOOSE}-{n}" for n in range(26)])
    assert exc.value.code == "INVALID_REQUEST"
    assert exc.value.http_status == 400


def test_over_the_reference_cap_is_rejected_before_anything_is_written():
    _seed_pair()
    from entities.services.merge import service as svc
    monkey = svc.MAX_REFERENCES
    svc.MAX_REFERENCES = 0
    try:
        case = Case.objects.create(
            title="Jhapa case", slug="jhapa-cap", case_type=CaseType.CORRUPTION,
            state=CaseState.DRAFT, short_description="t", description="t",
        )
        CaseEntityRelationship.objects.create(
            case=case, nes_id=LOOSE, relationship_type=RelationshipType.LOCATION
        )
        with pytest.raises(MergeError) as exc:
            _merge()
        assert exc.value.code == "MERGE_TOO_LARGE"
        assert exc.value.http_status == 409
        assert exc.value.extra["reference_count"] == 1
        assert StoredEntity.objects.get(pk=LOOSE).is_deleted is False
    finally:
        svc.MAX_REFERENCES = monkey


def test_conflicting_terminal_verdicts_are_rejected_before_anything_is_written():
    _seed_pair()
    case = Case.objects.create(
        title="Jhapa case", slug="jhapa-verdicts", case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT, short_description="t", description="t",
    )
    CaseEntityRelationship.objects.create(
        case=case, nes_id=JHAPA, relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.ACQUITTED,
    )
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.ACCUSED,
        outcome=RelationshipOutcome.CONVICTED,
    )
    with pytest.raises(MergeError) as exc:
        _merge()
    assert exc.value.code == "OUTCOME_CONFLICT"
    assert exc.value.http_status == 409
    # An acquitted defendant must never be rendered as convicted by a merge.
    assert StoredEntity.objects.get(pk=LOOSE).is_deleted is False


def test_a_partial_merge_resumes_on_the_next_attempt():
    _seed_pair()
    case = Case.objects.create(
        title="Jhapa case", slug="jhapa-resume", case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT, short_description="t", description="t",
    )
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.LOCATION
    )
    court = Court.objects.create(
        identifier="jhapadc", court_type="district",
        full_name_nepali="जिल्ला अदालत झापा", full_name_english="District Court Jhapa",
    )
    CourtCase.objects.create(case_number="081-CR-0081", court=court, nes_id=LOOSE)

    merge_id = _crash_once_in_court_rows()

    # Tombstone landed, so the retired IRI already resolves...
    assert StoredEntity.objects.get(pk=LOOSE).merged_into == JHAPA
    assert EntityMerge.objects.get(pk=merge_id).status == EntityMerge.PENDING
    # ...and the case bind that had already moved must not move twice.
    assert CaseEntityRelationship.objects.filter(nes_id=JHAPA).count() == 1

    result = _merge()
    assert result["status"] == "complete"
    assert EntityMerge.objects.get(pk=merge_id).status == EntityMerge.COMPLETE
    assert CaseEntityRelationship.objects.filter(nes_id=JHAPA).count() == 1
    assert not CaseEntityRelationship.objects.filter(nes_id=LOOSE).exists()
    # The store the first attempt never reached must be repointed by the resume.
    assert not CourtCase.objects.filter(nes_id=LOOSE).exists()
    assert CourtCase.objects.filter(nes_id=JHAPA).count() == 1


def test_a_dry_run_against_a_half_finished_merge_writes_nothing():
    _seed_pair()
    case = Case.objects.create(
        title="Jhapa case", slug="jhapa-dryresume", case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT, short_description="t", description="t",
    )
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.LOCATION
    )
    _crash_once_in_court_rows()

    result = _merge(dry_run=True)
    assert result["status"] == "planned"
    assert result["merge_id"] is None
    assert EntityMerge.objects.filter(status=EntityMerge.PENDING).count() == 1


def test_a_still_failing_sweep_stops_the_request_before_it_stalls_a_second_merge():
    # During an outage this leaves one stalled record instead of one per attempt,
    # and the second request's duplicate is never tombstoned.
    _seed_pair()
    _seed("location", "jhapa-2", "Place")
    other = "https://jawafdehi.org/entity/location/jhapa-2"
    _crash_once_in_court_rows(duplicate_iris=[LOOSE])
    _crash_once_in_court_rows(duplicate_iris=[other])
    assert EntityMerge.objects.filter(status=EntityMerge.PENDING).count() == 1
    assert StoredEntity.objects.get(pk=other).is_deleted is False


def test_every_stalled_merge_for_one_survivor_is_resumed():
    # Two PENDING records for one survivor now only arise from legacy data, since the
    # sweep runs before a new record is created — but the resume loop takes a list.
    _seed_pair()
    _seed("location", "jhapa-2", "Place")
    other = "https://jawafdehi.org/entity/location/jhapa-2"
    court = Court.objects.create(
        identifier="jhapapair", court_type="district",
        full_name_nepali="जिल्ला अदालत झापा", full_name_english="District Court Jhapa",
    )
    CourtCase.objects.create(case_number="081-CR-0101", court=court, nes_id=LOOSE)
    CourtCase.objects.create(case_number="081-CR-0102", court=court, nes_id=other)

    records = []
    for iri in (LOOSE, other):
        row = StoredEntity.objects.get(pk=iri)
        row.is_deleted = True
        row.merged_into = JHAPA
        row.save(update_fields=["is_deleted", "merged_into"])
        records.append(
            EntityMerge.objects.create(
                survivor_iri=JHAPA, duplicate_iris=[iri], duplicate_snapshots={},
                survivor_snapshot_before={}, status=EntityMerge.PENDING,
                author_id="oidc:seed", change_description="stalled",
            )
        )

    result = _merge(duplicate_iris=[LOOSE])
    assert result["status"] == "complete"
    assert all(
        EntityMerge.objects.get(pk=r.pk).status == EntityMerge.COMPLETE for r in records
    )
    assert not CourtCase.objects.filter(nes_id=LOOSE).exists()
    assert not CourtCase.objects.filter(nes_id=other).exists()


def test_a_pending_merge_survives_its_survivor_being_merged_away():
    # A → S stalls, then S → T. Sending A → T must still finish A's repointing.
    _seed_pair()
    _seed("location/district", "jhapa-far-east", "AdministrativeArea")
    final = "https://jawafdehi.org/entity/location/district/jhapa-far-east"
    court = Court.objects.create(
        identifier="jhapafareast", court_type="district",
        full_name_nepali="जिल्ला अदालत झापा पूर्व", full_name_english="District Court Jhapa East",
    )
    CourtCase.objects.create(case_number="081-CR-0099", court=court, nes_id=LOOSE)
    _crash_once_in_court_rows()
    _merge(survivor_iri=final, duplicate_iris=[JHAPA])

    result = _merge(survivor_iri=final, duplicate_iris=[LOOSE])
    assert result["status"] == "complete"
    assert not CourtCase.objects.filter(nes_id=LOOSE).exists()
    assert not EntityMerge.objects.filter(status=EntityMerge.PENDING).exists()


def test_a_resume_keeps_the_first_attempts_manifest_entries():
    _seed_pair()
    case = Case.objects.create(
        title="Jhapa case", slug="jhapa-manifest", case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT, short_description="t", description="t",
    )
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.LOCATION
    )
    merge_id = _crash_once_in_court_rows()
    first = EntityMerge.objects.get(pk=merge_id).reference_manifest
    assert any(e["store"] == "case_entity_binds" for e in first)

    _merge()
    after = EntityMerge.objects.get(pk=merge_id).reference_manifest
    assert all(entry in after for entry in first)


def test_a_search_outage_warns_instead_of_failing_a_finished_merge():
    _seed_pair()
    case = Case.objects.create(
        title="Jhapa case", slug="jhapa-index", case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT, short_description="t", description="t",
    )
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.LOCATION
    )

    from cases import search_index as case_search
    original = case_search.index_now

    def _boom(*args, **kwargs):
        raise RuntimeError("opensearch unreachable")

    case_search.index_now = _boom
    try:
        result = _merge()
    finally:
        case_search.index_now = original

    assert result["status"] == "complete"
    assert any("re-index" in w for w in result["warnings"])
    assert EntityMerge.objects.get(pk=result["merge_id"]).status == EntityMerge.COMPLETE


def test_warns_when_the_survivor_looks_less_complete_than_the_duplicate():
    _seed("location", "jhapa", "Place")
    _seed("location/district", "jhapa-np0104", ["AdministrativeArea", "jawafdehi:District"],
          identifier=[{"@type": "PropertyValue", "propertyID": "ocha-pcode", "value": "NP0104"}],
          description={"ne": "झापा जिल्ला"})
    result = _merge(survivor_iri=LOOSE, duplicate_iris=[JHAPA], dry_run=True)
    assert result["warnings"]


def test_a_stalled_merge_is_swept_up_by_the_next_merge_into_the_same_survivor():
    # The stalled attempt names a different duplicate, so no other path would find it.
    _seed_pair()
    _seed("location", "jhapa-2", "Place")
    other = "https://jawafdehi.org/entity/location/jhapa-2"
    court = Court.objects.create(
        identifier="jhapasweep", court_type="district",
        full_name_nepali="जिल्ला अदालत झापा", full_name_english="District Court Jhapa",
    )
    CourtCase.objects.create(case_number="081-CR-0099", court=court, nes_id=LOOSE)
    stalled = _crash_once_in_court_rows(duplicate_iris=[LOOSE])

    result = _merge(duplicate_iris=[other])
    assert result["status"] == "complete"
    assert EntityMerge.objects.get(pk=stalled).status == EntityMerge.COMPLETE
    assert not CourtCase.objects.filter(nes_id=LOOSE).exists()


def test_a_neighbour_that_no_longer_validates_does_not_wedge_the_merge():
    _seed_pair()
    neighbour = "https://jawafdehi.org/entity/location/localunit/damak-10502"
    # A stored entity with an @type validate_jsonld_entity no longer accepts —
    # written directly so it bypasses the create-time gate, the way a since-tightened
    # rule would leave a document that already validated at write time.
    StoredEntity.objects.create(
        iri=neighbour, entity_type="Wizard", prefix="location/localunit", slug="damak-10502",
        data={"@id": neighbour, "@type": "Wizard", "name": {"en": "Damak"},
              "containedInPlace": {"@id": LOOSE}},
    )
    result = _merge()
    assert result["status"] == "complete"
    row = StoredEntity.objects.get(pk=neighbour)
    assert row.data["containedInPlace"]["@id"] == JHAPA


def test_a_reference_to_another_retired_duplicate_is_dropped_from_the_survivor():
    _seed("location/district", "jhapa-np0104", ["AdministrativeArea", "jawafdehi:District"])
    other = "https://jawafdehi.org/entity/location/jhapa-2"
    _seed("location", "jhapa", "Place", containedInPlace={"@id": other})
    _seed("location", "jhapa-2", "Place")

    result = _merge(duplicate_iris=[LOOSE, other])
    assert result["status"] == "complete"

    survivor = StoredEntity.objects.get(pk=JHAPA)
    without_same_as = {k: v for k, v in survivor.data.items() if k != "sameAs"}
    assert other not in json.dumps(without_same_as)
    assert any("removed" in w for w in result["warnings"])


def test_only_the_cases_the_merge_touched_are_reindexed():
    _seed_pair()
    untouched = Case.objects.create(
        title="Jhapa case", slug="jhapa-untouched", case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT, short_description="t", description="t",
    )
    CaseEntityRelationship.objects.create(
        case=untouched, nes_id=JHAPA, relationship_type=RelationshipType.LOCATION
    )
    touched = Case.objects.create(
        title="Jhapa case", slug="jhapa-touched", case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT, short_description="t", description="t",
    )
    CaseEntityRelationship.objects.create(
        case=touched, nes_id=LOOSE, relationship_type=RelationshipType.LOCATION
    )

    from cases import search_index as case_search

    original = case_search.index_now
    indexed = []
    case_search.index_now = lambda case, **kwargs: indexed.append(case.id)
    try:
        result = _merge()
    finally:
        case_search.index_now = original

    assert result["status"] == "complete"
    assert indexed == [touched.id]


def test_a_duplicate_retired_concurrently_is_rejected():
    # Simulates the concurrent write landing right as this merge's phase one starts:
    # EntityMerge.objects.create is the first thing _phase_one does after _classify
    # has already seen the duplicate live.
    _seed_pair()
    from entities.services.merge import service as svc

    original_create = svc.EntityMerge.objects.create

    def _tombstone_then_create(*args, **kwargs):
        row = StoredEntity.objects.get(pk=LOOSE)
        row.is_deleted = True
        row.merged_into = "https://jawafdehi.org/entity/location/elsewhere"
        row.save(update_fields=["is_deleted", "merged_into"])
        return original_create(*args, **kwargs)

    svc.EntityMerge.objects.create = _tombstone_then_create
    try:
        with pytest.raises(MergeError) as exc:
            _merge()
        assert exc.value.code == "DUPLICATE_ALREADY_MERGED"
    finally:
        svc.EntityMerge.objects.create = original_create


def test_republishing_a_retired_entity_clears_its_merge_pointer():
    _seed_pair()
    _merge()
    dup = StoredEntity.objects.get(pk=LOOSE)
    assert dup.is_deleted is True
    assert dup.merged_into == JHAPA

    PublicationService().create_entity(
        doc={"@id": LOOSE, "@type": "Place", "name": {"en": "Jhapa (again)"}},
        author_id="oidc:seed", change_description="republish",
    )
    row = StoredEntity.objects.get(pk=LOOSE)
    assert row.is_deleted is False
    assert not row.merged_into
