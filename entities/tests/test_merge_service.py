"""EntityMergeService — phases, rejections, idempotency, resume."""

import pytest

from cases.models import (
    Case, CaseEntityRelationship, CaseState, CaseType, RelationshipOutcome, RelationshipType,
)
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
    assert result["total_references"] == 0
    assert StoredEntity.objects.get(pk=JHAPA).version == 2
    assert EntityMerge.objects.count() == 1


def test_self_merge_is_rejected():
    _seed_pair()
    with pytest.raises(MergeError) as exc:
        _merge(duplicate_iris=[JHAPA])
    assert exc.value.code == "SELF_MERGE"


def test_cross_family_merge_is_rejected():
    _seed("location/district", "jhapa-np0104", "AdministrativeArea")
    _seed("person", "ram-bahadur", "Person")
    with pytest.raises(MergeError) as exc:
        _merge(duplicate_iris=["https://jawafdehi.org/entity/person/ram-bahadur"])
    assert exc.value.code == "TYPE_MISMATCH"
    assert StoredEntity.objects.get(pk="https://jawafdehi.org/entity/person/ram-bahadur").is_deleted is False


def test_declared_type_family_must_agree_with_the_survivor():
    _seed_pair()
    with pytest.raises(MergeError) as exc:
        _merge(type_family="person")
    assert exc.value.code == "TYPE_MISMATCH"


def test_unknown_duplicate_is_a_not_found():
    _seed("location/district", "jhapa-np0104", "AdministrativeArea")
    with pytest.raises(MergeError) as exc:
        _merge(duplicate_iris=["https://jawafdehi.org/entity/location/nowhere"])
    assert exc.value.code == "NOT_FOUND"


def test_merging_into_a_retired_survivor_is_rejected():
    _seed_pair()
    _seed("location", "jhapa-older", "Place")
    _merge()
    with pytest.raises(MergeError) as exc:
        _merge(survivor_iri=LOOSE,
               duplicate_iris=["https://jawafdehi.org/entity/location/jhapa-older"])
    assert exc.value.code == "SURVIVOR_RETIRED"
    assert exc.value.extra["merged_into"] == JHAPA


def test_duplicate_already_merged_elsewhere_is_a_conflict():
    _seed_pair()
    _seed("location/district", "kaski-np0439", "AdministrativeArea")
    _merge()
    with pytest.raises(MergeError) as exc:
        _merge(survivor_iri="https://jawafdehi.org/entity/location/district/kaski-np0439")
    assert exc.value.code == "DUPLICATE_ALREADY_MERGED"


def test_too_many_duplicates_is_rejected():
    _seed_pair()
    with pytest.raises(MergeError) as exc:
        _merge(duplicate_iris=[f"{LOOSE}-{n}" for n in range(26)])
    assert exc.value.code == "INVALID_REQUEST"


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

    from entities.services.merge import service as svc
    original = svc.references.repoint_court_rows

    def _boom(*args, **kwargs):
        raise RuntimeError("ngm unreachable")

    svc.references.repoint_court_rows = _boom
    try:
        with pytest.raises(MergeError) as exc:
            _merge()
        assert exc.value.code == "MERGE_INCOMPLETE"
        merge_id = exc.value.extra["merge_id"]
    finally:
        svc.references.repoint_court_rows = original

    # Tombstone landed, so the retired IRI already resolves...
    assert StoredEntity.objects.get(pk=LOOSE).merged_into == JHAPA
    assert EntityMerge.objects.get(pk=merge_id).status == EntityMerge.PENDING
    # ...and the case bind that had already moved must not move twice.
    assert CaseEntityRelationship.objects.filter(nes_id=JHAPA).count() == 1

    result = _merge()
    assert result["status"] in ("complete", "already_merged")
    assert CaseEntityRelationship.objects.filter(nes_id=JHAPA).count() == 1
    assert not CaseEntityRelationship.objects.filter(nes_id=LOOSE).exists()


def test_warns_when_the_survivor_looks_less_complete_than_the_duplicate():
    _seed("location", "jhapa", "Place")
    _seed("location/district", "jhapa-np0104", ["AdministrativeArea", "jawafdehi:District"],
          identifier=[{"@type": "PropertyValue", "propertyID": "ocha-pcode", "value": "NP0104"}],
          description={"ne": "झापा जिल्ला"})
    result = _merge(survivor_iri=LOOSE, duplicate_iris=[JHAPA], dry_run=True)
    assert result["warnings"]
