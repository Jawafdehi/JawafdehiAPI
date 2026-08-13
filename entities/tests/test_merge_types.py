"""The merge's one type rule, and the advisory prefix check (pure logic, no DB)."""

from entities.services.merge.types import (
    is_person, normalize_type_token, prefix_mismatch, prefix_root, types_compatible,
)

JHAPA = "https://jawafdehi.org/entity/location/district/jhapa-np0104"
LOOSE = "https://jawafdehi.org/entity/location/jhapa"
KALIKOT = "https://jawafdehi.org/entity/kalikot/some-office"
CIAA = "https://jawafdehi.org/entity/organization/government/ciaa/head-office"
RAM = "https://jawafdehi.org/entity/person/ram-bahadur-thapa"


def test_a_person_may_not_merge_with_a_non_person():
    assert not types_compatible({"@type": "Person"}, {"@type": "GovernmentOrganization"})
    assert not types_compatible({"@type": "Place"}, {"@type": "Person"})


def test_two_people_merge():
    assert types_compatible({"@type": "Person"}, {"@type": "Person"})


def test_the_two_jhapas_merge_despite_different_types():
    # The duplicate this endpoint exists for: same district, different type spelling.
    # An exact-match rule would refuse it, and most other location duplicates too.
    survivor = {"@type": ["AdministrativeArea", "jawafdehi:District"]}
    duplicate = {"@type": "Place"}
    assert types_compatible(survivor, duplicate)


def test_unrelated_non_person_types_merge():
    # Deliberate: only the person boundary is load-bearing. A Hospital is legitimately
    # both an organization and a place, and nothing here has to know that.
    assert types_compatible({"@type": "Hospital"}, {"@type": "Organization"})
    assert types_compatible({"@type": "CreativeWork"}, {"@type": "Place"})


def test_an_unknown_type_is_treated_as_not_a_person():
    # What keeps this from going stale: a token added to the vocabulary needs no edit
    # here, and defaults to the answer that cannot fold a human into an office.
    assert not is_person({"@type": "SomeTypeAddedNextYear"})
    assert types_compatible({"@type": "SomeTypeAddedNextYear"}, {"@type": "Place"})
    assert not types_compatible({"@type": "SomeTypeAddedNextYear"}, {"@type": "Person"})


def test_every_accepted_spelling_of_person_is_recognised():
    for spelling in (
        "Person",
        "schema:Person",
        "https://schema.org/Person",
        "http://schema.org/Person",
    ):
        assert is_person({"@type": spelling}), spelling


def test_a_person_inside_a_type_list_still_counts():
    assert is_person({"@type": ["Person", "jawafdehi:Contractor"]})


def test_a_missing_or_malformed_type_is_not_a_person():
    assert not is_person({})
    assert not is_person({"@type": None})
    assert not is_person({"@type": [None, 7]})


def test_normalize_handles_the_jawafdehi_namespace():
    assert normalize_type_token("https://jawafdehi.org/ns#District") == "jawafdehi:District"
    assert normalize_type_token("jawafdehi:District") == "jawafdehi:District"


def test_prefix_root_is_the_first_segment():
    assert prefix_root(JHAPA) == "location"
    assert prefix_root(LOOSE) == "location"
    assert prefix_root(CIAA) == "organization"
    assert prefix_root("not-an-iri") == ""


def test_a_prefix_mismatch_is_only_reported_not_refused():
    # Folding a stray kalikot/... into location/district/... is the cleanup this
    # endpoint is for, so the mismatch warns. types_compatible still allows it.
    assert prefix_mismatch(JHAPA, KALIKOT)
    assert types_compatible({"@type": "AdministrativeArea"}, {"@type": "Place"})


def test_no_mismatch_between_a_prefix_and_its_own_subtree():
    assert not prefix_mismatch(JHAPA, LOOSE)


def test_an_unparseable_iri_never_reports_a_mismatch():
    assert not prefix_mismatch(JHAPA, "garbage")
    assert not prefix_mismatch("garbage", JHAPA)
