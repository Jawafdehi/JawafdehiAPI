"""The tombstone pointer, which lives in the retired entity's ``merged_into`` column."""

import pytest

from entities.models import StoredEntity
from entities.persistence import EntityRepository

JHAPA = "https://jawafdehi.org/entity/location/district/jhapa-np0104"
LOOSE = "https://jawafdehi.org/entity/location/jhapa"
OLDER = "https://jawafdehi.org/entity/location/jhapa-old"

# "__all__", never an explicit alias list: conftest.py documents that an explicit
# set enrolls the secondary connections for reads but not reliably for writes.
pytestmark = pytest.mark.django_db(databases="__all__")


def _entity(iri, merged_into=None, **kwargs):
    return StoredEntity.objects.create(
        iri=iri, entity_type="Place", prefix="location", slug=iri.rsplit("/", 1)[-1],
        data={"@id": iri, "@type": "Place", "name": {"en": "Jhapa"}},
        merged_into=merged_into, **kwargs
    )


def test_resolve_tombstone_returns_none_for_a_live_entity():
    _entity(JHAPA)
    assert EntityRepository().resolve_tombstone(JHAPA) is None


def test_a_soft_deleted_entity_without_a_pointer_is_not_a_tombstone():
    # DELETE /api/entities/{ref} flips is_deleted and writes no pointer; that row
    # must 404, not redirect.
    _entity(LOOSE, is_deleted=True)
    assert EntityRepository().resolve_tombstone(LOOSE) is None


def test_a_live_entity_carrying_the_pointer_is_not_a_tombstone():
    # A pointer only means anything on a retired row; the read side is inert otherwise.
    _entity(JHAPA)
    _entity(LOOSE, merged_into=JHAPA)
    assert EntityRepository().resolve_tombstone(LOOSE) is None


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
