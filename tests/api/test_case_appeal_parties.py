"""The public case payload says which entities are parties to each appeal.

``appeal_parties`` maps every appeal stage's ``courtcase_iri`` to the NES ids on
that docket: rows linked by hand, plus each of the case's accused whose name
pairs with a docket row. It is worked out when the case is served, so it is what
lets a multi-accused case attribute the docket's verdict to the accused who were
on the appeal, and to nobody else. Sibling of ``appeal_verdicts``.
"""

import pytest
from django.db import connections
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from cases.models import CaseEntityRelationship, RelationshipType
from courts.models import CaseEntity
from entities.models import StoredEntity
from tests.api.test_case_appeal_verdicts import SPECIAL, SUPREME, _appealed_case, _case, _court, _detail

RATAN = "https://jawafdehi.org/entity/person/ratan-bahadur-shahi"
JAGAT = "https://jawafdehi.org/entity/person/jagat-bahadur-malla"


def _accused(case, iri, name):
    prefix, slug = iri.split("/entity/", 1)[1].rsplit("/", 1)
    StoredEntity.objects.get_or_create(iri=iri, defaults=dict(
        entity_type="Person", prefix=prefix, slug=slug,
        data={"@id": iri, "@type": "Person", "name": name}))
    CaseEntityRelationship.objects.create(
        case=case, nes_id=iri, relationship_type=RelationshipType.ACCUSED)


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


@pytest.mark.django_db
def test_an_accused_named_on_the_docket_is_listed_without_a_hand_link():
    _party("supreme", "076-CR-1565", "नेपाल सरकार", side="plaintiff")
    _party("supreme", "076-CR-1565", "रतनबहादुर शाही")
    case = _appealed_case()
    _accused(case, RATAN, {"ne": "रतन बहादुर शाही"})
    _accused(case, JAGAT, {"ne": "जगत बहादुर मल्ल"})

    assert _detail()["appeal_parties"] == {SUPREME: [RATAN]}


@pytest.mark.django_db
def test_an_accused_shown_in_english_is_matched_by_the_nepali_name():
    _party("supreme", "076-CR-1565", "रतन बहादुर शाही")
    case = _appealed_case()
    _accused(case, RATAN, {"en": "Ratan Bahadur Shahi", "ne": "रतन बहादुर शाही"})

    assert _detail()["appeal_parties"] == {SUPREME: [RATAN]}


@pytest.mark.django_db
def test_the_list_matches_names_too():
    _party("supreme", "076-CR-1565", "रतनबहादुर शाही")
    case = _appealed_case()
    _accused(case, RATAN, {"en": "Ratan Bahadur Shahi", "ne": "रतन बहादुर शाही"})

    (row,) = APIClient().get("/api/cases/").data["results"]
    assert row["appeal_parties"] == {SUPREME: [RATAN]}


@pytest.mark.django_db
def test_two_accused_sharing_a_name_are_not_matched():
    _party("supreme", "076-CR-1565", "रतन बहादुर शाही")
    case = _appealed_case()
    _accused(case, RATAN, {"ne": "रतन बहादुर शाही"})
    _accused(case, JAGAT, {"ne": "रतनबहादुर शाही"})

    assert _detail()["appeal_parties"] == {SUPREME: []}


@pytest.mark.django_db
def test_matching_adds_no_entity_query_per_card():
    client = APIClient()

    def _add(index):
        number = f"076-CR-{index:04d}"
        iri = f"https://jawafdehi.org/courtcase/supreme/{number.lower()}"
        person = f"https://jawafdehi.org/entity/person/accused-{index}"
        _party("supreme", number, f"रतन बहादुर शाही {index}")
        case = _appealed_case(slug=f"appeal-names-{index}", appeal_iri=iri)
        _accused(case, person, {"ne": f"रतन बहादुर शाही {index}"})

    _add(1)
    with CaptureQueriesContext(connections["nes"]) as one_card:
        assert client.get("/api/cases/").status_code == 200
    for index in range(2, 6):
        _add(index)
    with CaptureQueriesContext(connections["nes"]) as five_cards:
        assert client.get("/api/cases/").status_code == 200

    assert len(five_cards) <= len(one_card)
