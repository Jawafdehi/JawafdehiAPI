"""Tombstone pointer + merge audit record."""

import pytest

from entities.models import EntityMerge, StoredEntity
from entities.persistence import EntityRepository

JHAPA = "https://jawafdehi.org/entity/location/district/jhapa-np0104"
LOOSE = "https://jawafdehi.org/entity/location/jhapa"
OLDER = "https://jawafdehi.org/entity/location/jhapa-old"

# "__all__", never an explicit alias list: conftest.py documents that an explicit
# set enrolls the secondary connections for reads but not reliably for writes.
pytestmark = pytest.mark.django_db(databases="__all__")


def _entity(iri, **kwargs):
    return StoredEntity.objects.create(
        iri=iri, entity_type="Place", prefix="location", slug=iri.rsplit("/", 1)[-1],
        data={"@id": iri, "@type": "Place", "name": {"en": "Jhapa"}}, **kwargs
    )


def test_merged_into_defaults_to_null():
    assert _entity(JHAPA).merged_into is None


def test_resolve_tombstone_returns_none_for_a_live_entity():
    _entity(JHAPA)
    assert EntityRepository().resolve_tombstone(JHAPA) is None


def test_resolve_tombstone_follows_a_chain_to_the_final_survivor():
    _entity(JHAPA)
    _entity(LOOSE, is_deleted=True, merged_into=JHAPA)
    _entity(OLDER, is_deleted=True, merged_into=LOOSE)
    assert EntityRepository().resolve_tombstone(OLDER) == JHAPA


def test_resolve_tombstone_returns_a_target_that_no_longer_exists():
    ghost = "https://jawafdehi.org/entity/location/ghost"
    _entity(LOOSE, is_deleted=True, merged_into=ghost)
    # No row for `ghost` exists at all — checking it is live is the caller's job.
    assert EntityRepository().resolve_tombstone(LOOSE) == ghost


def test_resolve_tombstone_terminates_on_a_cycle():
    _entity(LOOSE, is_deleted=True, merged_into=OLDER)
    _entity(OLDER, is_deleted=True, merged_into=LOOSE)
    # The guarantee is bounded work, not a particular answer: with max_hops=3 the
    # walk is LOOSE→OLDER→LOOSE→OLDER and returns where it stopped.
    assert EntityRepository().resolve_tombstone(LOOSE, max_hops=3) == OLDER


def test_merge_record_round_trips_snapshots_and_manifest():
    merge = EntityMerge.objects.create(
        survivor_iri=JHAPA,
        duplicate_iris=[LOOSE],
        duplicate_snapshots={LOOSE: {"@id": LOOSE, "@type": "Place"}},
        survivor_snapshot_before={"@id": JHAPA, "@type": "AdministrativeArea"},
        status=EntityMerge.PENDING,
        author_id="oidc:377592055028777324",
        change_description="Two entities for one Jhapa district",
    )
    merge.refresh_from_db()
    assert merge.status == EntityMerge.PENDING
    assert merge.duplicate_snapshots[LOOSE]["@type"] == "Place"
    assert merge.reference_manifest == []
    assert merge.completed_at is None
