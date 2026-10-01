"""The public case payload carries each appeal's verdict, as the court recorded it.

``appeal_verdicts`` maps every appeal stage's ``courtcase_iri`` to that docket's
``verdict_type``, passed through unmapped: ``CLAIM_DENIED`` on an appeal means the
appeal failed, and saying so is the frontend's label, not the API's rewrite.

It is a sibling of ``dates``, never a key inside a stage record: the casework
enrichers PATCH back the stage list they read, and ``validate_stages`` refuses
unknown keys.
"""

import pytest
from django.db import connections
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from cases.models import Case, CaseState, CaseTrack, CaseType
from courts.models import Court, CourtCase

SPECIAL = "https://jawafdehi.org/courtcase/special/074-cr-0115"
SUPREME = "https://jawafdehi.org/courtcase/supreme/076-cr-1565"


def _court(identifier):
    return Court.objects.get_or_create(
        identifier=identifier,
        defaults={
            "court_type": identifier,
            "full_name_nepali": identifier,
            "full_name_english": identifier,
        },
    )[0]


def _docket(court, case_number, verdict_type):
    return CourtCase.objects.create(
        case_number=case_number, court=_court(court), verdict_type=verdict_type
    )


def _case(slug, stages, court_cases):
    case = Case(
        title="Appeal verdict case",
        slug=slug,
        offence_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        description="d",
        short_description="s",
        case_track=CaseTrack.CIAA,
        dates={"stages": stages},
    )
    case.court_cases = court_cases
    case.save()
    return case


def _appealed_case(slug="appeal-verdict", appeal_iri=SUPREME):
    return _case(
        slug,
        [
            {"stage": "initial", "courtcase_iri": SPECIAL, "start": "2018-01-01", "end": "2019-01-01"},
            {"stage": "appeal", "courtcase_iri": appeal_iri, "start": "2020-02-24", "end": "2024-03-07"},
        ],
        [SPECIAL, appeal_iri],
    )


def _detail(slug="appeal-verdict"):
    return APIClient().get(f"/api/cases/{slug}/").data


@pytest.mark.django_db
def test_detail_passes_the_court_verdict_through_unmapped():
    _docket("special", "074-CR-0115", "CONVICTED")
    _docket("supreme", "076-CR-1565", "CLAIM_DENIED")
    _appealed_case()

    assert _detail()["appeal_verdicts"] == {SUPREME: "CLAIM_DENIED"}


@pytest.mark.django_db
def test_a_pending_appeal_maps_to_null():
    _docket("supreme", "076-CR-1565", None)
    _appealed_case()

    assert _detail()["appeal_verdicts"] == {SUPREME: None}


@pytest.mark.django_db
def test_an_appeal_with_no_court_row_maps_to_null():
    _appealed_case()

    assert _detail()["appeal_verdicts"] == {SUPREME: None}


@pytest.mark.django_db
def test_raw_portal_text_in_the_column_is_not_published():
    """A bench referral reads like a disposition but means the case is live."""
    _docket("supreme", "076-CR-1565", "पूर्ण इजलासमा पेस हुने")
    _appealed_case()

    assert _detail()["appeal_verdicts"] == {SUPREME: None}


@pytest.mark.django_db
def test_a_case_without_an_appeal_has_an_empty_map():
    _docket("special", "074-CR-0115", "CONVICTED")
    _case(
        "no-appeal",
        [{"stage": "initial", "courtcase_iri": SPECIAL, "start": "2018-01-01"}],
        [SPECIAL],
    )

    assert _detail("no-appeal")["appeal_verdicts"] == {}


@pytest.mark.django_db
def test_an_appeal_heard_below_the_supreme_court_resolves_too():
    high = "https://jawafdehi.org/courtcase/patanhc/077-cr-0042"
    _docket("patanhc", "077-CR-0042", "REVERSED")
    _appealed_case(appeal_iri=high)

    assert _detail()["appeal_verdicts"] == {high: "REVERSED"}


@pytest.mark.django_db
def test_the_list_carries_it_without_a_court_query_per_card():
    client = APIClient()

    def _add(index):
        number = f"076-CR-{index:04d}"
        iri = f"https://jawafdehi.org/courtcase/supreme/{number.lower()}"
        _docket("supreme", number, "AFFIRMED")
        _appealed_case(slug=f"appeal-list-{index}", appeal_iri=iri)
        return iri

    first = _add(1)
    with CaptureQueriesContext(connections["ngm"]) as one_card:
        rows = client.get("/api/cases/").data["results"]
    assert rows[0]["appeal_verdicts"] == {first: "AFFIRMED"}

    for index in range(2, 6):
        _add(index)
    with CaptureQueriesContext(connections["ngm"]) as five_cards:
        assert client.get("/api/cases/").status_code == 200

    assert len(five_cards) <= len(one_card), (
        f"court queries scaled with cards: 1 card={len(one_card)}, "
        f"5 cards={len(five_cards)}"
    )
