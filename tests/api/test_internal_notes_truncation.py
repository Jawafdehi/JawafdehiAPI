"""Integration tests: `internal_notes` is published but truncated on read.

The Jawafdehi API is open source and internal_notes is not a secret, so it IS
exposed on the case detail (GET) and list endpoints. But it is capped to
`INTERNAL_NOTES_PREVIEW_CHARS` so those payloads never ship an unbounded
internal blob; the full value is used by the review pipeline and set via PATCH.
"""

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from cases.models import CaseState, CaseType
from cases.serializers import INTERNAL_NOTES_PREVIEW_CHARS
from tests.conftest import create_case_with_entities

SHORT = "NO_BIGO: record_offence — आरोपपत्रमा बिगो रकम उल्लेख छैन"
LONG = "x" * (INTERNAL_NOTES_PREVIEW_CHARS + 50)
LIST_URL = "/api/cases/"
DETAIL_URL = "/api/cases/{}/"


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


def _published_case(internal_notes):
    case = create_case_with_entities(
        title="Internal notes truncation case",
        alleged_entities=["entity:person/test-person"],
        key_allegations=["Test"],
        case_type=CaseType.CORRUPTION,
        description="A published case used to assert internal_notes truncation.",
        internal_notes=internal_notes,
    )
    case.state = CaseState.PUBLISHED
    case.save()
    return case


def _list_row(response, case_id):
    data = response.json()
    rows = data["results"] if isinstance(data, dict) and "results" in data else data
    return next((r for r in rows if r["id"] == case_id), None)


@pytest.mark.django_db
def test_short_internal_notes_returned_in_full_on_detail():
    case = _published_case(SHORT)
    resp = APIClient().get(DETAIL_URL.format(case.slug))
    assert resp.status_code == 200
    assert resp.json()["internal_notes"] == SHORT


@pytest.mark.django_db
def test_internal_notes_present_and_truncated_on_detail():
    case = _published_case(LONG)
    resp = APIClient().get(DETAIL_URL.format(case.slug))
    assert resp.status_code == 200
    value = resp.json()["internal_notes"]
    assert value.endswith("…")
    assert len(value) == INTERNAL_NOTES_PREVIEW_CHARS + 1  # cap + ellipsis
    assert value != LONG


@pytest.mark.django_db
def test_internal_notes_present_and_truncated_on_list():
    case = _published_case(LONG)
    resp = APIClient().get(LIST_URL)
    assert resp.status_code == 200
    row = _list_row(resp, case.id)
    assert row is not None
    assert "internal_notes" in row
    assert row["internal_notes"].endswith("…")
    assert len(row["internal_notes"]) == INTERNAL_NOTES_PREVIEW_CHARS + 1


@pytest.mark.django_db
def test_empty_internal_notes_is_empty_string():
    case = _published_case("")
    resp = APIClient().get(DETAIL_URL.format(case.slug))
    assert resp.status_code == 200
    assert resp.json()["internal_notes"] == ""
