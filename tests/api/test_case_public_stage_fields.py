"""The public case payload has to carry the stage fields the site renders.

The case page no longer renders one "Case date" range; it renders one row per
proceeding stage, and it takes the lifecycle chip from the API because no
client can derive it (there is no single end date to read it off). Those live
on the model and on the caseworker PATCH snapshot -- this pins them on the
PUBLIC read, list and detail, for anonymous callers.

Without them the frontend does not fail loudly: ``caseStages`` returns an empty
list for an absent ``dates``, so every case page silently renders NO dates at
all.
"""

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseTrack,
    CaseType,
    RelationshipOutcome,
    RelationshipType,
)

SPECIAL = "https://jawafdehi.org/courtcase/special/081-cr-0060"
SUPREME = "https://jawafdehi.org/courtcase/supreme/081-ns-1234"

STAGES = [
    {"stage": "investigation", "body": "अख्तियार", "start": "2021-03-14", "end": "2022-01-09"},
    {"stage": "initial", "courtcase_iri": SPECIAL, "start": "2022-01-10", "end": "2024-05-22"},
    {
        "stage": "appeal",
        "courtcase_iri": SUPREME,
        "start": "2024-06-20",
        "notes": "पुनरावेदन विचाराधीन छ।",
    },
]


def _published_case(slug="stage-fields", stages=None, **kwargs) -> Case:
    case = Case(
        title="Stage fields case",
        slug=slug,
        offence_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        description="d",
        short_description="s",
        case_track=CaseTrack.CIAA,
        dates={"stages": stages if stages is not None else STAGES},
        **kwargs,
    )
    case.court_cases = [SPECIAL, SUPREME]
    case.save()
    return case


@pytest.mark.django_db
def test_detail_serves_the_stage_list_to_an_anonymous_reader():
    """``dates.stages`` is what the case page renders one row per."""
    _published_case()

    data = APIClient().get("/api/cases/stage-fields/").data

    assert data["dates"] == {"stages": STAGES}


@pytest.mark.django_db
def test_detail_serves_the_derived_lifecycle_and_track():
    """The chip cannot be derived client-side -- there is no single end date."""
    _published_case()

    data = APIClient().get("/api/cases/stage-fields/").data

    # An open appeal outranks the decided first instance.
    assert data["status"] == "ongoing"
    assert data["case_track"] == "ciaa"


@pytest.mark.django_db
def test_detail_serves_the_derived_span():
    """Surfaces with room for one date read these, not the deprecated pair."""
    _published_case()

    data = APIClient().get("/api/cases/stage-fields/").data

    assert data["proceedings_started_on"] == "2022-01-10"
    # NULL while any court stage is still open.
    assert data["proceedings_decided_on"] is None


@pytest.mark.django_db
def test_the_list_carries_them_too():
    """Entity and court-case pages date their cards off the list payload."""
    _published_case()

    row = APIClient().get("/api/cases/").data["results"][0]

    assert row["dates"] == {"stages": STAGES}
    assert row["status"] == "ongoing"
    assert row["proceedings_started_on"] == "2022-01-10"


@pytest.mark.django_db
def test_a_status_override_wins_on_the_public_read():
    """``dormant`` is the whole reason the override column exists."""
    case = _published_case(stages=[{"stage": "initial", "start": "2019-02-02"}])
    case.status_override = "dormant"
    case.save()

    data = APIClient().get("/api/cases/stage-fields/").data

    assert data["status"] == "dormant"
    # Returned in its own right, so the editor round-trips it instead of
    # trying to read it back out of the derived value.
    assert data["status_override"] == "dormant"


@pytest.mark.django_db
def test_the_lifecycle_does_not_cost_a_query_per_card():
    """``Case.status`` reads the accused binds, which the list prefetches.

    A ``.filter()`` on a prefetched related manager ignores the prefetch cache
    and issues a fresh query, so the obvious implementation puts the list back
    to one query per card.

    Every stage here is CLOSED on purpose: an open court stage short-circuits
    the derivation before it ever reads a bind, so a card with a pending
    appeal would pass this test no matter how the binds are fetched.
    """
    client = APIClient()
    decided = [{"stage": "initial", "start": "2022-01-10", "end": "2024-05-22"}]

    def _case(index):
        case = _published_case(slug=f"stage-perf-{index}", stages=decided)
        CaseEntityRelationship.objects.create(
            case=case,
            nes_id=f"https://jawafdehi.org/entity/person/accused-{index}",
            relationship_type=RelationshipType.ACCUSED,
            outcome=RelationshipOutcome.ACQUITTED,
        )
        return case

    _case(1)
    with CaptureQueriesContext(connection) as one_card:
        assert client.get("/api/cases/").status_code == 200

    for index in range(2, 6):
        _case(index)
    with CaptureQueriesContext(connection) as five_cards:
        assert client.get("/api/cases/").status_code == 200

    assert len(five_cards) <= len(one_card) + 1, (
        f"query count scaled with cards: 1 card={len(one_card)}, "
        f"5 cards={len(five_cards)}"
    )
