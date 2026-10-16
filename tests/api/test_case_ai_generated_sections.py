"""
Tests for ``Case.ai_generated_sections``: the per-section "AI-generated, not yet
reviewed" label.

Contract the frontend builds against: JSON key ``ai_generated_sections``, a
``string[]`` drawn from {description, key_allegations, timeline}, returned on the
public list and detail responses and writable via JSON Patch on any state.
"""

from typing import TYPE_CHECKING

import pytest
from auditlog.models import LogEntry
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from hypothesis import given, settings
from hypothesis import strategies as st
from rest_framework.test import APIClient

from cases.fields import AI_GENERATED_SECTIONS, normalize_ai_generated_sections
from cases.models import Case, CaseState, CaseType
from tests.conftest import create_user_with_role

if TYPE_CHECKING:
    from django.contrib.auth.models import User
else:
    User = get_user_model()

URL = "/api/cases/{}/"
PATH = "/ai_generated_sections"


def _make_case(**kwargs) -> Case:
    defaults = dict(
        title="भ्रष्टाचार मुद्दा",
        offence_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        description="मुद्दाको विवरण",
        short_description="छोटो विवरण",
        key_allegations=["राम बहादुर थापाले रकम हिनामिना गरेको"],
        timeline=[{"date": "2024-01-01", "title": "मुद्दा दर्ता"}],
    )
    defaults.update(kwargs)
    return Case.objects.create(**defaults)


def _client(user=None) -> APIClient:
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


def _caseworker(name="sita") -> User:
    return create_user_with_role(name, f"{name}@example.com", "Caseworker")


def _patch(client, case, ops, **extra):
    return client.patch(URL.format(case.slug), data=ops, format="json", **extra)


# ---------------------------------------------------------------------------
# Model field
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_defaults_to_empty_list():
    assert _make_case().ai_generated_sections == []


@given(st.lists(st.sampled_from(AI_GENERATED_SECTIONS), unique=True))
def test_model_accepts_any_duplicate_free_subset(sections):
    field = Case._meta.get_field("ai_generated_sections")
    field.validate(sections, None)


@pytest.mark.parametrize(
    "value",
    [
        ["title"],
        ["description", "Description"],
        [""],
        [1],
        "description",
        {"description": True},
        ["timeline", "timeline"],
    ],
)
def test_model_rejects_unknown_non_list_and_duplicate_values(value):
    field = Case._meta.get_field("ai_generated_sections")
    with pytest.raises(ValidationError):
        field.validate(value, None)


@given(st.lists(st.sampled_from(AI_GENERATED_SECTIONS)))
def test_normalize_is_a_canonical_ordered_set(values):
    result = normalize_ai_generated_sections(values)
    assert set(result) == set(values)
    assert len(result) == len(set(result))
    assert result == [s for s in AI_GENERATED_SECTIONS if s in result]
    assert normalize_ai_generated_sections(result) == result


# ---------------------------------------------------------------------------
# Public read
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_public_list_and_detail_include_the_field():
    case = _make_case(
        state=CaseState.PUBLISHED, ai_generated_sections=["description", "timeline"]
    )
    client = _client()

    listing = client.get("/api/cases/")
    assert listing.status_code == 200
    row = next(r for r in listing.json()["results"] if r["slug"] == case.slug)
    assert row["ai_generated_sections"] == ["description", "timeline"]

    detail = client.get(URL.format(case.slug))
    assert detail.status_code == 200
    assert detail.json()["ai_generated_sections"] == ["description", "timeline"]


@pytest.mark.django_db
def test_public_detail_returns_empty_list_for_an_unlabelled_case():
    case = _make_case(state=CaseState.PUBLISHED)
    detail = _client().get(URL.format(case.slug))
    assert detail.json()["ai_generated_sections"] == []


# ---------------------------------------------------------------------------
# PATCH
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_patch_sets_and_clears():
    case = _make_case()
    client = _client(_caseworker())

    resp = _patch(
        client,
        case,
        [{"op": "replace", "path": PATH, "value": ["timeline", "description"]}],
    )
    assert resp.status_code == 200, resp.content
    # Stored and echoed in canonical order.
    assert resp.json()["ai_generated_sections"] == ["description", "timeline"]
    case.refresh_from_db()
    assert case.ai_generated_sections == ["description", "timeline"]

    resp = _patch(client, case, [{"op": "replace", "path": PATH, "value": []}])
    assert resp.status_code == 200, resp.content
    case.refresh_from_db()
    assert case.ai_generated_sections == []


@pytest.mark.django_db
def test_patch_removes_one_label_by_index():
    case = _make_case(ai_generated_sections=list(AI_GENERATED_SECTIONS))
    resp = _patch(
        _client(_caseworker()), case, [{"op": "remove", "path": f"{PATH}/1"}]
    )
    assert resp.status_code == 200, resp.content
    case.refresh_from_db()
    assert case.ai_generated_sections == ["description", "timeline"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "value", [["title"], ["description", "summary"], "description", [None]]
)
def test_patch_rejects_invalid_values(value):
    case = _make_case(ai_generated_sections=["timeline"])
    resp = _patch(
        _client(_caseworker()), case, [{"op": "replace", "path": PATH, "value": value}]
    )
    # The endpoint's established status for a body that fails validation.
    assert resp.status_code == 422
    assert "ai_generated_sections" in resp.json()
    case.refresh_from_db()
    assert case.ai_generated_sections == ["timeline"]


@pytest.mark.django_db
def test_patch_folds_duplicates_so_append_is_idempotent():
    case = _make_case(ai_generated_sections=["timeline"])
    client = _client(_caseworker())
    for _ in range(2):
        resp = _patch(
            client, case, [{"op": "add", "path": f"{PATH}/-", "value": "timeline"}]
        )
        assert resp.status_code == 200, resp.content
    case.refresh_from_db()
    assert case.ai_generated_sections == ["timeline"]


@pytest.mark.django_db
def test_patch_append_unions_with_existing_names():
    case = _make_case(ai_generated_sections=["timeline"])
    resp = _patch(
        _client(_caseworker()),
        case,
        [{"op": "add", "path": f"{PATH}/-", "value": "description"}],
    )
    assert resp.status_code == 200, resp.content
    case.refresh_from_db()
    assert case.ai_generated_sections == ["description", "timeline"]


@pytest.mark.django_db
def test_patch_works_on_a_published_case_with_if_match():
    case = _make_case(
        state=CaseState.PUBLISHED, ai_generated_sections=list(AI_GENERATED_SECTIONS)
    )
    client = _client(_caseworker())
    token = client.get(URL.format(case.slug)).headers["ETag"]

    resp = _patch(
        client,
        case,
        [{"op": "replace", "path": PATH, "value": ["key_allegations"]}],
        HTTP_IF_MATCH=token,
    )
    assert resp.status_code == 200, resp.content
    case.refresh_from_db()
    assert case.state == CaseState.PUBLISHED
    assert case.ai_generated_sections == ["key_allegations"]


@pytest.mark.django_db
def test_patch_with_a_stale_etag_is_refused():
    case = _make_case(ai_generated_sections=["description"])
    client = _client(_caseworker())
    stale = client.get(URL.format(case.slug)).headers["ETag"]
    _patch(client, case, [{"op": "replace", "path": "/title", "value": "नयाँ शीर्षक"}])

    resp = _patch(
        client, case, [{"op": "replace", "path": PATH, "value": []}], HTTP_IF_MATCH=stale
    )
    assert resp.status_code == 412
    case.refresh_from_db()
    assert case.ai_generated_sections == ["description"]


@pytest.mark.django_db
def test_patch_is_audit_logged_with_the_actor():
    user = _caseworker("gita")
    case = _make_case(ai_generated_sections=["description", "timeline"])

    resp = _patch(_client(user), case, [{"op": "remove", "path": f"{PATH}/0"}])
    assert resp.status_code == 200, resp.content

    entry = (
        LogEntry.objects.get_for_object(case)
        .filter(action=LogEntry.Action.UPDATE)
        .first()
    )
    assert entry is not None
    assert entry.actor == user
    assert "ai_generated_sections" in entry.changes


@pytest.mark.django_db
@pytest.mark.parametrize(
    "op",
    [
        {"op": "replace", "path": "/description", "value": "सच्याइएको विवरण"},
        {"op": "replace", "path": "/key_allegations", "value": ["सच्याइएको आरोप"]},
        {
            "op": "replace",
            "path": "/timeline",
            "value": [{"date": "2024-02-01", "title": "फैसला"}],
        },
    ],
)
def test_editing_section_text_does_not_clear_the_label(op):
    sections = list(AI_GENERATED_SECTIONS)
    case = _make_case(ai_generated_sections=sections)
    resp = _patch(_client(_caseworker()), case, [op])
    assert resp.status_code == 200, resp.content
    case.refresh_from_db()
    assert case.ai_generated_sections == sections


@pytest.mark.django_db
def test_patch_requires_authentication():
    case = _make_case()
    resp = _patch(_client(), case, [{"op": "replace", "path": PATH, "value": []}])
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Enricher write shape, end to end
# ---------------------------------------------------------------------------


@pytest.mark.django_db
@settings(max_examples=20, deadline=None)
@given(
    existing=st.lists(st.sampled_from(AI_GENERATED_SECTIONS), unique=True),
    section=st.sampled_from(AI_GENERATED_SECTIONS),
)
def test_enricher_ops_union_with_existing_labels(existing, section):
    """The exact ops `CaseworkApi.patch_field(..., ai_sections=...)` sends never drop a name."""
    from casework.common.api import build_mark_ai_generated_ops, build_replace_patch

    value = {
        "description": "एआईले लेखेको विवरण",
        "key_allegations": ["एआईले लेखेको आरोप"],
        "timeline": [{"date": "2024-03-01", "title": "आरोपपत्र दायर"}],
    }[section]
    case = _make_case(ai_generated_sections=existing)
    ops = build_replace_patch(section, value) + build_mark_ai_generated_ops([section])

    resp = _patch(_client(_caseworker()), case, ops)
    assert resp.status_code == 200, resp.content
    case.refresh_from_db()
    assert set(case.ai_generated_sections) == set(existing) | {section}
    assert getattr(case, section) == value
