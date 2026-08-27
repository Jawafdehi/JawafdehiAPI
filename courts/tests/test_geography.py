"""Coverage pin for courts/geography.py: every court the scraper tables know
must resolve to a location, and the province table must be exactly the
7-province structure — so a court added to ``court_ids.py`` can never silently
index without a district/province, and a spelling drift between the two tables
is a red test, not an empty facet bucket.
"""

from __future__ import annotations

from courts.geography import (
    DISTRICT_PROVINCE,
    HIGH_COURT_SEATS,
    NATIONAL,
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


def test_every_high_court_resolves_via_its_seat_district():
    for court in HIGH_COURTS:
        location = court_location(court["identifier"])
        assert location is not None, court["identifier"]
        district, province = location
        assert district == HIGH_COURT_SEATS[court["identifier"]]
        assert district in DISTRICT_PROVINCE
        assert province == DISTRICT_PROVINCE[district]
    # And the seat table carries no stale identifiers the scraper no longer knows.
    assert set(HIGH_COURT_SEATS) == {c["identifier"] for c in HIGH_COURTS}


def test_district_province_is_exactly_the_77_district_7_province_structure():
    assert len(DISTRICT_PROVINCE) == 77
    assert set(DISTRICT_PROVINCE.values()) == PROVINCES


def test_national_courts_get_the_sentinel_in_both_positions():
    """Supreme + special court have national jurisdiction — "no location" must be
    a visible, filterable group, not an absent field."""
    assert court_location("supreme") == (NATIONAL, NATIONAL)
    assert court_location("special") == (NATIONAL, NATIONAL)
    assert NATIONAL == "NATIONAL"  # upper-case: collision-proof with real names


def test_unknown_identifier_resolves_to_none_not_a_guess():
    assert court_location("atlantisdc") is None
    assert court_location("") is None
