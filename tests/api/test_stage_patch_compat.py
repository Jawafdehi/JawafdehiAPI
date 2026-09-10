"""Writing stages through PATCH, and the deprecated date paths the SPA sends.

The deployed caseworker admin emits ``/case_start_date`` and ``/case_end_date``.
Those addressed columns; the case's dates now live inside a stage record, and
RFC-6902 cannot rewrite a path onto a field inside a list that may not exist.
So the two paths are transformed rather than rewritten, and the requirement is
that the admin never loses date editing.
"""

import pytest
from rest_framework.test import APIClient

from cases.models import Case, CaseState, CaseType
from tests.conftest import create_user_with_role

URL = "/api/cases/{}/"


def _case(**kwargs) -> Case:
    defaults = dict(
        title="Stage patch case",
        offence_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        description="d",
        short_description="s",
    )
    defaults.update(kwargs)
    return Case.objects.create(**defaults)


def _client():
    client = APIClient()
    client.force_authenticate(user=create_user_with_role(
        f"cw{_client.n}", f"cw{_client.n}@example.com", "Caseworker"))
    _client.n += 1
    return client


_client.n = 0


def _patch(case, ops):
    return _client().patch(URL.format(case.slug), ops, format="json")


# ── the new path ─────────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_patching_dates_persists_the_stage_list():
    case = _case()

    response = _patch(case, [{
        "op": "replace",
        "path": "/dates",
        "value": {"stages": [
            {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}
        ]},
    }])

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.dates["stages"][0]["stage"] == "initial"


@pytest.mark.django_db
def test_patching_dates_recomputes_the_derived_columns():
    """The scalar write path is a bulk UPDATE that skips save(), so the derived
    columns have to be recomputed explicitly or they silently misorder the
    archive on every date edit."""
    case = _case()

    _patch(case, [{
        "op": "replace",
        "path": "/dates",
        "value": {"stages": [
            {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}
        ]},
    }])

    case.refresh_from_db()
    assert str(case.proceedings_started_on) == "2024-02-25"
    assert str(case.proceedings_decided_on) == "2025-08-13"


@pytest.mark.django_db
def test_an_open_appeal_clears_the_derived_decision_through_patch():
    case = _case(dates={"stages": [
        {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}
    ]})

    _patch(case, [{
        "op": "replace",
        "path": "/dates",
        "value": {"stages": [
            {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"},
            {"stage": "appeal", "start": "2025-09-20"},
        ]},
    }])

    case.refresh_from_db()
    assert case.proceedings_decided_on is None


@pytest.mark.django_db
def test_an_unknown_stage_is_refused_with_422_naming_it():
    case = _case()

    response = _patch(case, [{
        "op": "replace", "path": "/dates",
        "value": {"stages": [{"stage": "first_instance"}]},
    }])

    assert response.status_code == 422, response.data
    assert "first_instance" in str(response.data)


# ── the deprecated paths ─────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_deprecated_start_path_updates_the_initial_stage():
    case = _case(dates={"stages": [
        {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}
    ]})

    response = _patch(case, [
        {"op": "replace", "path": "/case_start_date", "value": "2024-03-01"}
    ])

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.dates["stages"][0]["start"] == "2024-03-01"
    assert case.dates["stages"][0]["end"] == "2025-08-13"
    assert str(case.proceedings_started_on) == "2024-03-01"


@pytest.mark.django_db
def test_the_deprecated_path_creates_the_stage_when_there_is_none():
    """RFC-6902 replace on a missing path is an error, which is exactly why
    this is a transform and not a path rewrite."""
    case = _case()

    response = _patch(case, [
        {"op": "replace", "path": "/case_start_date", "value": "2024-02-25"}
    ])

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert case.dates["stages"] == [{"stage": "initial", "start": "2024-02-25"}]


@pytest.mark.django_db
def test_the_deprecated_path_can_clear_a_date():
    case = _case(dates={"stages": [
        {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}
    ]})

    response = _patch(case, [
        {"op": "replace", "path": "/case_end_date", "value": None}
    ])

    assert response.status_code == 200, response.data
    case.refresh_from_db()
    assert "end" not in case.dates["stages"][0]
    assert case.proceedings_decided_on is None


@pytest.mark.django_db
def test_the_deprecated_path_is_refused_when_the_case_has_several_first_instances():
    """A case with parallel first instances cannot be expressed by the old
    form at all, so guessing which stage it meant would corrupt data."""
    case = _case(dates={"stages": [
        {"stage": "initial", "start": "2024-01-10"},
        {"stage": "initial", "start": "2024-01-10"},
    ]})

    response = _patch(case, [
        {"op": "replace", "path": "/case_start_date", "value": "2024-03-01"}
    ])

    assert response.status_code == 422, response.data
    assert "case_start_date" in response.data


@pytest.mark.django_db
def test_a_backwards_deprecated_write_is_refused():
    case = _case(dates={"stages": [
        {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}
    ]})

    response = _patch(case, [
        {"op": "replace", "path": "/case_end_date", "value": "2020-01-01"}
    ])

    assert response.status_code == 422, response.data


# ── the read aliases ─────────────────────────────────────────────────────────


@pytest.mark.django_db
def test_the_read_aliases_come_from_the_initial_stage():
    from cases.serializers import CaseSerializer

    case = _case(dates={"stages": [
        {"stage": "investigation", "start": "2023-01-12", "end": "2024-02-25"},
        {"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"},
    ]})

    data = CaseSerializer(case).data

    assert data["case_start_date"] == "2024-02-25", "investigation must not leak"
    assert data["case_end_date"] == "2025-08-13"


@pytest.mark.django_db
def test_the_read_aliases_are_null_without_a_first_instance():
    from cases.serializers import CaseSerializer

    data = CaseSerializer(_case()).data

    assert data["case_start_date"] is None
    assert data["case_end_date"] is None


@pytest.mark.django_db
def test_creating_with_the_deprecated_dates_lands_a_first_instance_stage():
    """Otherwise the case reads as having no proceedings at all and sorts by
    created_at, which is routinely wrong by months."""
    from cases.caseworker_serializers import CaseCreateSerializer

    serializer = CaseCreateSerializer(
        data={
            "title": "Created with old dates",
            "offence_type": CaseType.CORRUPTION,
            "case_start_date": "2024-02-25",
            "case_end_date": "2025-08-13",
        }
    )

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["dates"] == {
        "stages": [{"stage": "initial", "start": "2024-02-25", "end": "2025-08-13"}]
    }


@pytest.mark.django_db
def test_creating_with_backwards_deprecated_dates_is_refused():
    from cases.caseworker_serializers import CaseCreateSerializer

    serializer = CaseCreateSerializer(
        data={
            "title": "Backwards",
            "offence_type": CaseType.CORRUPTION,
            "case_start_date": "2024-02-25",
            "case_end_date": "2020-01-01",
        }
    )

    assert not serializer.is_valid()
    assert "case_end_date" in serializer.errors
