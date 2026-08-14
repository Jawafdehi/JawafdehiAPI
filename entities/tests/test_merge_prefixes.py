"""The merge's one advisory check (pure logic, no DB)."""

from entities.services.merge.prefixes import prefix_mismatch, prefix_root

JHAPA = "https://jawafdehi.org/entity/location/district/jhapa-np0104"
LOOSE = "https://jawafdehi.org/entity/location/jhapa"
KALIKOT = "https://jawafdehi.org/entity/kalikot/some-office"
CIAA = "https://jawafdehi.org/entity/organization/government/ciaa/head-office"
RAM = "https://jawafdehi.org/entity/person/ram-bahadur-thapa"


def test_prefix_root_is_the_first_segment():
    assert prefix_root(JHAPA) == "location"
    assert prefix_root(LOOSE) == "location"
    assert prefix_root(CIAA) == "organization"
    assert prefix_root(RAM) == "person"


def test_an_unparseable_iri_has_no_root():
    assert prefix_root("not-an-iri") == ""
    assert prefix_root("") == ""


def test_no_mismatch_between_a_prefix_and_its_own_subtree():
    # The production duplicate this endpoint exists for.
    assert not prefix_mismatch(JHAPA, LOOSE)


def test_a_stray_prefix_is_reported():
    # kalikot/, mahabai/ and deptofsurvey/ are real roots in production holding
    # entities that belong under a canonical prefix. Folding them is the job, so this
    # is reported and never refused.
    assert prefix_mismatch(JHAPA, KALIKOT)


def test_a_person_against_a_non_person_is_reported():
    # Nothing here knows what a person is. The naming convention carries it: a human
    # lives under person/, so the pairing worth a second look reads as a mismatch.
    assert prefix_mismatch(RAM, CIAA)
    assert prefix_mismatch(RAM, JHAPA)
    assert not prefix_mismatch(RAM, "https://jawafdehi.org/entity/person/ram-b-thapa")


def test_an_unparseable_iri_never_reports_a_mismatch():
    assert not prefix_mismatch(JHAPA, "garbage")
    assert not prefix_mismatch("garbage", JHAPA)
