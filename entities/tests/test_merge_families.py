"""Type-family compatibility for entity merges (pure logic, no DB)."""

from entities.services.merge.families import (
    families_compatible,
    families_for,
    normalize_type_token,
)


def _doc(atype):
    return {"@id": "https://jawafdehi.org/entity/location/jhapa", "@type": atype}


def test_normalizes_prefixed_and_iri_type_forms():
    assert normalize_type_token("schema:Person") == "Person"
    assert normalize_type_token("https://schema.org/Person") == "Person"
    assert normalize_type_token("https://jawafdehi.org/ns#District") == "jawafdehi:District"
    assert normalize_type_token("jawafdehi:District") == "jawafdehi:District"


def test_the_two_real_jhapa_entities_are_compatible():
    # The exact production pair: location/jhapa vs location/district/jhapa-np0104.
    loose = _doc("Place")
    canonical = _doc(["AdministrativeArea", "jawafdehi:District"])
    assert families_compatible(loose, canonical)


def test_person_never_merges_with_organization():
    assert not families_compatible(_doc("Person"), _doc("Organization"))


def test_hospital_belongs_to_both_place_and_organization():
    assert families_for(_doc("Hospital")) == frozenset({"place", "organization"})
    assert families_compatible(_doc("Hospital"), _doc("GovernmentOrganization"))
    assert families_compatible(_doc("Hospital"), _doc("Place"))


def test_thing_is_the_open_world_wildcard():
    assert families_compatible(_doc("Thing"), _doc("Person"))
    assert families_compatible(_doc("Person"), _doc("Thing"))


def test_unknown_type_is_incompatible_with_everything():
    assert not families_compatible(_doc("Sandwich"), _doc("Person"))
