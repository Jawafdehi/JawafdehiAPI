"""The public case payload says which entities are parties to each appeal.

``appeal_parties`` maps every appeal stage's ``courtcase_iri`` to the NES ids
resolved onto that docket's party rows. It is what lets a multi-accused case
attribute the docket's verdict to the accused who were on the appeal, and to
nobody else. Sibling of ``appeal_verdicts``, for the same reason.
"""

import pytest
from django.db import connections
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from courts.models import CaseEntity
from tests.api.test_case_appeal_verdicts import SPECIAL, SUPREME, _appealed_case, _case, _court, _detail

RATAN = "https://jawafdehi.org/entity/person/ratan-bahadur-shahi"
JAGAT = "https://jawafdehi.org/entity/person/jagat-bahadur-malla"


def _party(court, case_number, name, nes_id=None, side="defendant"):
    return CaseEntity.objects.create(
        court=_court(court), case_number=case_number, side=side, name=name, nes_id=nes_id
    )


@pytest.mark.django_db
def test_detail_lists_the_entities_resolved_on_the_appeal_docket():
    _party("supreme", "076-CR-1565", "जगत बहादुर मल्ल", JAGAT)
    _party("supreme", "076-CR-1565", "रतन बहादुर शाही", RATAN)
    _appealed_case()

    assert _detail()["appeal_parties"] == {SUPREME: [JAGAT, RATAN]}


@pytest.mark.django_db
def test_unresolved_party_rows_are_left_out():
    _party("supreme", "076-CR-1565", "नेपाल सरकार", None, side="plaintiff")
    _party("supreme", "076-CR-1565", "रतन बहादुर शाही", RATAN)
    _party("supreme", "076-CR-1565", "जगत बहादुर मल्ल", "")
    _appealed_case()

    assert _detail()["appeal_parties"] == {SUPREME: [RATAN]}


@pytest.mark.django_db
def test_an_appeal_with_no_resolved_party_maps_to_an_empty_list():
    _party("supreme", "076-CR-1565", "रतन बहादुर शाही", None)
    _appealed_case()

    assert _detail()["appeal_parties"] == {SUPREME: []}


@pytest.mark.django_db
def test_a_party_resolved_on_the_trial_docket_is_not_an_appeal_party():
    _party("special", "074-CR-0115", "रतन बहादुर शाही", RATAN)
    _appealed_case()

    assert _detail()["appeal_parties"] == {SUPREME: []}


@pytest.mark.django_db
def test_an_entity_named_twice_on_one_docket_is_listed_once():
    _party("supreme", "076-CR-1565", "रतन बहादुर शाही", RATAN)
    _party("supreme", "076-CR-1565", "रतनबहादुर शाही", RATAN)
    _appealed_case()

    assert _detail()["appeal_parties"] == {SUPREME: [RATAN]}


@pytest.mark.django_db
def test_a_case_without_an_appeal_has_an_empty_map():
    _case(
        "no-appeal",
        [{"stage": "initial", "courtcase_iri": SPECIAL, "start": "2018-01-01"}],
        [SPECIAL],
    )

    assert _detail("no-appeal")["appeal_parties"] == {}


@pytest.mark.django_db
def test_the_list_carries_it_without_a_court_query_per_card():
    client = APIClient()

    def _add(index):
        number = f"076-CR-{index:04d}"
        iri = f"https://jawafdehi.org/courtcase/supreme/{number.lower()}"
        _party("supreme", number, "रतन बहादुर शाही", RATAN)
        _appealed_case(slug=f"appeal-list-{index}", appeal_iri=iri)
        return iri

    first = _add(1)
    with CaptureQueriesContext(connections["ngm"]) as one_card:
        rows = client.get("/api/cases/").data["results"]
    assert rows[0]["appeal_parties"] == {first: [RATAN]}

    for index in range(2, 6):
        _add(index)
    with CaptureQueriesContext(connections["ngm"]) as five_cards:
        assert client.get("/api/cases/").status_code == 200

    assert len(five_cards) <= len(one_card), (
        f"court queries scaled with cards: 1 card={len(one_card)}, "
        f"5 cards={len(five_cards)}"
    )
