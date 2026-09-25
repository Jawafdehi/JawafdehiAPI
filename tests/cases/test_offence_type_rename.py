"""The `case_type` -> `offence_type` rename contract.

`Case.case_type` and `CourtCase.case_type` meant two different things under one
name: the offence (10 enum values) and NGM's free-text Nepali मुद्दा string
(130k distinct values). Both reached the search index as a field literally
called `case_type`. This module pins the case side of the rename and the
compatibility surface that keeps the deployed SPA working for one release.
"""

import pytest
from rest_framework.test import APIClient

from cases.models import Case, CaseState, CaseType
from tests.conftest import create_user_with_role

URL = "/api/cases/{}/"


def _make_case(**kwargs) -> Case:
    defaults = dict(
        title="Test case",
        offence_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        description="Some description",
        short_description="Short",
    )
    defaults.update(kwargs)
    return Case.objects.create(**defaults)


def _authed_client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


@pytest.mark.django_db
def test_the_offence_is_stored_on_offence_type():
    case = _make_case()

    assert case.offence_type == CaseType.CORRUPTION
    assert Case.objects.get(pk=case.pk).offence_type == CaseType.CORRUPTION


@pytest.mark.django_db
def test_the_read_serializer_still_answers_to_case_type():
    """One-release read alias: the deployed SPA reads `case_type`."""
    from cases.serializers import CaseSerializer

    data = CaseSerializer(_make_case()).data

    assert data["offence_type"] == CaseType.CORRUPTION
    assert data["case_type"] == CaseType.CORRUPTION


@pytest.mark.django_db
def test_patch_refuses_to_change_the_offence():
    """The offence is set at create and blocked from PATCH; renaming the field
    must not open a write path that did not exist."""
    case = _make_case()
    user = create_user_with_role("cw1", "cw1@example.com", "Caseworker")

    response = _authed_client(user).patch(
        URL.format(case.slug),
        [{"op": "replace", "path": "/offence_type", "value": CaseType.BRIBERY}],
        format="json",
    )

    assert response.status_code == 422, response.data
    case.refresh_from_db()
    assert case.offence_type == CaseType.CORRUPTION


@pytest.mark.django_db
def test_patch_still_refuses_the_deprecated_case_type_path():
    """The deployed SPA emits `/case_type`. It was blocked before the rename and
    has to stay blocked, not fall through the blocklist and be silently dropped."""
    case = _make_case()
    user = create_user_with_role("cw2", "cw2@example.com", "Caseworker")

    response = _authed_client(user).patch(
        URL.format(case.slug),
        [{"op": "replace", "path": "/case_type", "value": CaseType.FORGERY}],
        format="json",
    )

    assert response.status_code == 422, response.data
    case.refresh_from_db()
    assert case.offence_type == CaseType.CORRUPTION


@pytest.mark.django_db
def test_the_list_filter_answers_to_both_names():
    """`?case_type=` is a public query parameter the deployed SPA sends."""
    _make_case(state=CaseState.PUBLISHED, slug="corruption-one")
    _make_case(
        state=CaseState.PUBLISHED, slug="bribery-one", offence_type=CaseType.BRIBERY
    )
    client = APIClient()

    new = client.get("/api/cases/?offence_type=CORRUPTION")
    old = client.get("/api/cases/?case_type=CORRUPTION")

    assert new.status_code == 200 and old.status_code == 200
    assert [c["slug"] for c in new.data["results"]] == ["corruption-one"]
    assert [c["slug"] for c in old.data["results"]] == ["corruption-one"]


@pytest.mark.django_db
def test_create_accepts_the_deprecated_case_type():
    """POST create is a hard cut in the note, but the deployed SPA admin creates
    cases with `case_type` and must not start 400ing mid-release."""
    from cases.caseworker_serializers import CaseCreateSerializer

    serializer = CaseCreateSerializer(
        data={"title": "Old client case", "case_type": CaseType.BRIBERY}
    )

    assert serializer.is_valid(), serializer.errors
    assert serializer.validated_data["offence_type"] == CaseType.BRIBERY


@pytest.mark.django_db
def test_the_index_doc_carries_both_names_for_one_release():
    """`?case_type=` filters case docs AND courtcase docs from one parameter.

    Renaming only the case side would leave that filter silently returning
    courtcase hits alone, so case docs emit both names until the frontend and
    any saved queries have moved to `offence_type`."""
    from cases.search_index import build_indexed_doc

    doc = build_indexed_doc(_make_case(state=CaseState.PUBLISHED))

    assert doc["offence_type"] == CaseType.CORRUPTION
    assert doc["case_type"] == CaseType.CORRUPTION
