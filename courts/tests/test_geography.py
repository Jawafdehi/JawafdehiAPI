"""Coverage pin for courts/geography.py: every court the scraper tables know
must resolve to a location, and the province table must be exactly the
7-province structure — so a court added to ``court_ids.py`` can never silently
index without a district/province, and a spelling drift between the two tables
is a red test, not an empty facet bucket.
"""

from __future__ import annotations

from courts.geography import (
    ALL_COURT_IDENTIFIERS,
    DISTRICT_PROVINCE,
    HIGH_COURT_SEATS,
    NATIONAL,
    NATIONAL_COURTS,
    court_location,
)
from courts.scraper.court_ids import DISTRICT_COURTS, HIGH_COURTS

PROVINCES = {
    "Koshi",
    "Madhesh",
    "Bagmati",
    "Gandaki",
    "Lumbini",
    "Karnali",
    "Sudurpashchim",
}


def test_every_district_court_resolves_to_a_known_district_and_province():
    for court in DISTRICT_COURTS:
        location = court_location(court["code_name"])
        assert location is not None, court["code_name"]
        district, province = location
        assert district == court["district_en"]
        assert DISTRICT_PROVINCE[district] == province


def test_every_high_court_resolves_to_its_seats_province_and_no_district():
    """A high court is a PROVINCIAL court. Its seat is used only to borrow the
    province; the seat district itself is NOT indexed, because it would answer
    "which town is the bench in" rather than "whose case is this"."""
    for court in HIGH_COURTS:
        location = court_location(court["identifier"])
        assert location is not None, court["identifier"]
        district, province = location
        assert district is None, court["identifier"]
        seat = HIGH_COURT_SEATS[court["identifier"]]
        assert seat in DISTRICT_PROVINCE
        assert province == DISTRICT_PROVINCE[seat]
    # And the seat table carries no stale identifiers the scraper no longer knows.
    assert set(HIGH_COURT_SEATS) == {c["identifier"] for c in HIGH_COURTS}


def test_district_is_only_ever_a_district_courts_own_district():
    """The whole point of the district facet: ?district=Kathmandu must mean
    Kathmandu District Court, not "any court that happens to sit in Kathmandu"."""
    districts = {
        identifier: court_location(identifier)[0]
        for identifier in ALL_COURT_IDENTIFIERS
    }
    with_a_district = {i for i, d in districts.items() if d is not None}
    assert with_a_district == {c["code_name"] for c in DISTRICT_COURTS}


def test_province_covers_every_court_including_the_national_ones():
    """Province is the fallback geography, so no court may be unreachable by it."""
    for identifier in ALL_COURT_IDENTIFIERS:
        province = court_location(identifier)[1]
        assert province in PROVINCES | {NATIONAL}, identifier


def test_district_province_is_exactly_the_77_district_7_province_structure():
    assert len(DISTRICT_PROVINCE) == 77
    assert set(DISTRICT_PROVINCE.values()) == PROVINCES


def test_national_courts_get_the_sentinel_province_and_no_district():
    """Supreme + special court have national jurisdiction: no district at all,
    and a sentinel province so "national" is a visible, filterable group rather
    than an absent field."""
    assert court_location("supreme") == (None, NATIONAL)
    assert court_location("special") == (None, NATIONAL)
    assert NATIONAL == "NATIONAL"  # upper-case: collision-proof with real names


def test_all_court_identifiers_is_exactly_the_97_courts_that_exist():
    """This set is the search API's closed ``?court=`` vocabulary, so an omission
    is a court whose cases silently cannot be filtered for."""
    assert len(ALL_COURT_IDENTIFIERS) == 97
    assert ALL_COURT_IDENTIFIERS == (
        {c["code_name"] for c in DISTRICT_COURTS}
        | {c["identifier"] for c in HIGH_COURTS}
        | set(NATIONAL_COURTS)
    )
    # Every member resolves — the vocabulary cannot advertise a court the
    # indexer would then fail to place.
    for identifier in ALL_COURT_IDENTIFIERS:
        assert court_location(identifier) is not None, identifier


def test_unknown_identifier_resolves_to_none_not_a_guess():
    assert court_location("atlantisdc") is None
    assert court_location("") is None
