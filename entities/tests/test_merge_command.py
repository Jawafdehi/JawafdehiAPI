"""manage.py merge_entities — the escape hatch for merges over the API's cap."""

import json
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from cases.models import Case, CaseEntityRelationship, CaseState, CaseType, RelationshipType
from entities.models import StoredEntity
from entities.persistence import EntityRepository
from entities.services.merge import MergeError
from entities.services.publication import PublicationService
from entities.write_validation import normalize_authoring_payload

JHAPA = "https://jawafdehi.org/entity/location/district/jhapa-np0104"
LOOSE = "https://jawafdehi.org/entity/location/jhapa"

pytestmark = pytest.mark.django_db(databases="__all__")


def _seed(prefix, slug, atype):
    return PublicationService().create_entity(
        doc=normalize_authoring_payload(
            {"prefix": prefix, "slug": slug, "type": atype, "name": {"en": "Jhapa"}}
        ),
        author_id="oidc:seed", change_description="seed",
    )


def test_command_merges_and_tombstones():
    _seed("location/district", "jhapa-np0104", "AdministrativeArea")
    _seed("location", "jhapa", "Place")
    out = StringIO()
    call_command("merge_entities", survivor=JHAPA, duplicate=[LOOSE], stdout=out)
    assert EntityRepository().resolve_tombstone(LOOSE) == JHAPA
    summary = json.loads(out.getvalue())
    assert summary["status"] == "complete"
    assert summary["merge_id"]
    assert "survivor" not in summary


def test_dry_run_writes_nothing():
    _seed("location/district", "jhapa-np0104", "AdministrativeArea")
    _seed("location", "jhapa", "Place")
    call_command("merge_entities", survivor=JHAPA, duplicate=[LOOSE], dry_run=True)
    assert StoredEntity.objects.get(pk=LOOSE).is_deleted is False


def test_the_command_runs_a_merge_the_endpoint_would_refuse(monkeypatch):
    # The one thing this command exists for. The bind is load-bearing: with no
    # references at all the count is 0, and `0 > 0` would never trip the cap.
    from entities.services.merge import service as svc

    _seed("location/district", "jhapa-np0104", "AdministrativeArea")
    _seed("location", "jhapa", "Place")
    case = Case.objects.create(
        title="Jhapa case", slug="jhapa-overcap", case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT, short_description="t", description="t",
    )
    CaseEntityRelationship.objects.create(
        case=case, nes_id=LOOSE, relationship_type=RelationshipType.LOCATION
    )
    monkeypatch.setattr(svc, "MAX_REFERENCES", 0)

    with pytest.raises(MergeError) as exc:
        svc.EntityMergeService().merge(
            survivor_iri=JHAPA, duplicate_iris=[LOOSE], author_id="oidc:seed"
        )
    assert exc.value.code == "MERGE_TOO_LARGE"

    call_command("merge_entities", survivor=JHAPA, duplicate=[LOOSE])
    assert EntityRepository().resolve_tombstone(LOOSE) == JHAPA


def test_a_refused_merge_surfaces_as_a_command_error():
    _seed("location/district", "jhapa-np0104", "AdministrativeArea")
    with pytest.raises(CommandError) as exc:
        call_command("merge_entities", survivor=JHAPA, duplicate=[JHAPA])
    assert "SELF_MERGE" in str(exc.value)
