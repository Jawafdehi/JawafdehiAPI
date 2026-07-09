"""
Property-based tests for public API.

Feature: accountability-platform-core
Tests Properties 8, 10, 15, 16
Validates: Requirements 4.1, 6.1, 6.2, 6.3, 8.1, 8.3
"""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from rest_framework.test import APIClient

from cases.models import CaseMaterialReference, CaseState, CaseType
from tests.conftest import (
    create_case_with_entities,
)
from tests.strategies import (
    complete_case_data_with_timeline as complete_case_data,
)

VALID_MATERIAL_IRI = "https://jawafdehi.org/material/jawafdehi/20240115.ab12cd"

# ============================================================================
# Property 8: Public API only shows published cases
# ============================================================================


@pytest.mark.django_db
@settings(max_examples=20, deadline=800)
@given(
    case_data=complete_case_data(),
    state=st.sampled_from(
        [CaseState.DRAFT, CaseState.IN_REVIEW, CaseState.PUBLISHED, CaseState.CLOSED]
    ),
)
def test_public_api_only_shows_published_cases(case_data, state):
    """
    Feature: accountability-platform-core, Property 8: Public API only shows published cases

    For any API request to list cases, only cases with state=PUBLISHED should be returned.
    The detail endpoint also only returns PUBLISHED cases to the public; casework
    states (DRAFT, IN_REVIEW) require a casework-viewing role and otherwise 404.
    Validates: Requirements 6.1, 8.3
    """

    # Create a case with the given state
    case = create_case_with_entities(**case_data)
    case.state = state
    case.save()

    # Make API request to list cases
    client = APIClient()
    response = client.get("/api/cases/")

    # API should return 200 OK
    assert (
        response.status_code == 200
    ), f"API should return 200 OK, but got {response.status_code}"

    # Check if case appears in results
    case_ids_in_response = [c.get("slug") for c in response.data.get("results", [])]

    # List endpoint only shows PUBLISHED cases
    should_appear = state == CaseState.PUBLISHED

    if should_appear:
        # Cases should appear
        assert (
            case.slug in case_ids_in_response
        ), f"Case {case.slug} with state={state} should appear in API list response"
    else:
        # Cases should NOT appear
        assert (
            case.slug not in case_ids_in_response
        ), f"Case {case.slug} with state={state} should NOT appear in API list response"

    # Test detail endpoint - only PUBLISHED is publicly accessible; casework
    # states (DRAFT, IN_REVIEW) and CLOSED return 404 to anonymous callers.
    detail_response = client.get(f"/api/cases/{case.slug}/")

    if state == CaseState.PUBLISHED:
        assert (
            detail_response.status_code == 200
        ), "PUBLISHED case should be accessible via detail endpoint"
    else:
        # DRAFT, IN_REVIEW (casework), and CLOSED are not publicly accessible.
        assert (
            detail_response.status_code == 404
        ), f"{state} case should NOT be publicly accessible via detail endpoint"


# ============================================================================
# Property 10: Evidence is a material reference (CaseMaterialReference join)
# ============================================================================


@pytest.mark.django_db
@settings(max_examples=20, deadline=800)
@given(case_data=complete_case_data())
def test_evidence_exposes_material_references(case_data):
    """
    Feature: accountability-platform-core, Property 10: Evidence references materials

    Evidence is the CaseMaterialReference join. Each API evidence entry exposes a
    material_iri and additional_details; the detail endpoint additionally enriches
    it with a resolved nested `material` object.
    Validates: Requirements 4.1
    """
    # Create a case
    case = create_case_with_entities(**case_data)

    # Add a material reference (evidence)
    CaseMaterialReference.objects.create(
        case=case,
        material_iri=VALID_MATERIAL_IRI,
        additional_details="This material supports the allegation",
    )

    # Publish the case
    case.state = CaseState.PUBLISHED
    case.save()

    # Retrieve via API
    client = APIClient()
    response = client.get(f"/api/cases/{case.slug}/")

    assert response.status_code == 200

    # Check evidence is included
    evidence_list = response.data.get("evidence", [])
    assert (
        len(evidence_list) > 0
    ), "Published case should include evidence in API response"

    # Check evidence has required fields
    for evidence_item in evidence_list:
        assert "material_iri" in evidence_item, "Evidence should include material_iri"
        assert (
            "additional_details" in evidence_item
        ), "Evidence should include additional_details"
        assert evidence_item["material_iri"] == VALID_MATERIAL_IRI
        # Detail endpoint enriches evidence with a nested resolved material object
        assert "material" in evidence_item, "Detail endpoint should include material"
        assert "display_name" in evidence_item["material"]
        assert "material_type" in evidence_item["material"]
        assert "urls" in evidence_item["material"]


# ============================================================================
# Property 15: Search and filter functionality
# ============================================================================


@pytest.mark.django_db
@settings(max_examples=20, deadline=800)
@given(
    case_data=complete_case_data(),
    search_term=st.text(
        min_size=3,
        max_size=20,
        alphabet=st.characters(whitelist_categories=("Ll", "Lu")),
    ),
)
def test_search_functionality_across_fields(case_data, search_term):
    """
    Feature: accountability-platform-core, Property 15: Search and filter functionality

    For any search query on the public API, the Platform should return published cases
    matching the criteria across title, description, and key_allegations fields.
    Validates: Requirements 6.2, 8.1
    """
    # Ensure search term appears in at least one searchable field
    case_data["title"] = f"{search_term} Case Title"

    # Create and publish a case
    case = create_case_with_entities(**case_data)
    case.state = CaseState.PUBLISHED
    case.save()

    # Search for the term
    client = APIClient()
    response = client.get(f"/api/cases/?search={search_term}")

    assert response.status_code == 200

    # Case should appear in search results
    case_ids_in_response = [c.get("slug") for c in response.data.get("results", [])]
    assert (
        case.slug in case_ids_in_response
    ), f"Case with '{search_term}' in title should appear in search results"


@pytest.mark.django_db
@settings(max_examples=20, deadline=800)
@given(
    case_data=complete_case_data(),
    case_type=st.sampled_from([CaseType.CORRUPTION]),
)
def test_filter_by_case_type(case_data, case_type):
    """
    Feature: accountability-platform-core, Property 15: Search and filter functionality

    For any filter by case_type on the public API, the Platform should return
    only published cases matching that case_type.
    Validates: Requirements 6.2, 8.1
    """
    # Set the case type
    case_data["case_type"] = case_type

    # Create and publish a case
    case = create_case_with_entities(**case_data)
    case.state = CaseState.PUBLISHED
    case.save()

    # Filter by case_type
    client = APIClient()
    response = client.get(f"/api/cases/?case_type={case_type}")

    assert response.status_code == 200

    # All returned cases should have the filtered case_type
    for returned_case in response.data.get("results", []):
        assert (
            returned_case.get("case_type") == case_type
        ), f"Filtered results should only include case_type={case_type}"

    # Our case should appear in results
    case_ids_in_response = [c.get("slug") for c in response.data.get("results", [])]
    assert (
        case.slug in case_ids_in_response
    ), f"Case with case_type={case_type} should appear in filtered results"


@pytest.mark.django_db
@settings(max_examples=20, deadline=800)
@given(
    case_data=complete_case_data(),
    tag=st.text(
        alphabet=st.characters(
            whitelist_categories=("Ll", "Nd"), whitelist_characters="-"
        ),
        min_size=3,
        max_size=30,
    ).filter(lambda x: x and not x.startswith("-") and not x.endswith("-")),
)
def test_filter_by_tags(case_data, tag):
    """
    Feature: accountability-platform-core, Property 15: Search and filter functionality

    For any filter by tags on the public API, the Platform should return
    only published cases containing that tag.
    Validates: Requirements 6.2, 8.1
    """
    # Add the tag to the case
    case_data["tags"] = [tag]

    # Create and publish a case
    case = create_case_with_entities(**case_data)
    case.state = CaseState.PUBLISHED
    case.save()

    # Filter by tag
    client = APIClient()
    response = client.get(f"/api/cases/?tags={tag}")

    assert response.status_code == 200

    # Our case should appear in results
    case_ids_in_response = [c.get("slug") for c in response.data.get("results", [])]
    assert (
        case.slug in case_ids_in_response
    ), f"Case with tag '{tag}' should appear in filtered results"

    # All returned cases should have the tag
    for returned_case in response.data.get("results", []):
        returned_tags = returned_case.get("tags", [])
        assert (
            tag in returned_tags
        ), f"Filtered results should only include cases with tag '{tag}'"


@pytest.mark.django_db
def test_tag_filter_postgres_branch_uses_contains_lookup(monkeypatch):
    """The tag filter has TWO code paths (cases/api_views.py get_queryset):

        if connection.vendor == "postgresql":  # PROD
            queryset.filter(tags__contains=[tag])
        else:                                   # SQLite (CI test engine)
            <filter in Python>

    CI runs on SQLite, so the PROD ``tags__contains`` branch is otherwise NEVER
    exercised — a regression there (wrong lookup, wrong wrapping of the value)
    would ship green. This forces the postgres branch by faking
    ``connection.vendor`` and captures the ``tags__contains`` kwarg the view
    passes to ``.filter()`` — WITHOUT evaluating the queryset (the JSON contains
    lookup can't run on the sqlite test DB), so it is a pure branch-selection +
    argument-shape assertion.
    """
    from django.db.models import QuerySet
    from rest_framework.test import APIRequestFactory

    from cases.api_views import CaseViewSet

    captured = {}
    orig_filter = QuerySet.filter

    def _spy_filter(self, *args, **kwargs):
        if "tags__contains" in kwargs:
            # Record and SHORT-CIRCUIT: don't actually apply the postgres-only
            # lookup (it would error on sqlite). Returning self is fine — the view
            # only chains .prefetch_related().order_by() after this, which we also
            # never evaluate.
            captured["tags__contains"] = kwargs["tags__contains"]
            return self
        return orig_filter(self, *args, **kwargs)

    monkeypatch.setattr(QuerySet, "filter", _spy_filter)
    # ``connection`` in the view module is a ConnectionProxy; replace the whole
    # name with a stand-in whose .vendor is postgresql so the prod branch runs.
    from types import SimpleNamespace

    monkeypatch.setattr(
        "cases.api_views.connection", SimpleNamespace(vendor="postgresql")
    )

    from django.contrib.auth.models import AnonymousUser
    from rest_framework.request import Request

    raw = APIRequestFactory().get("/api/cases/", {"tags": "procurement"})
    request = Request(raw)  # gives .query_params
    request.user = AnonymousUser()  # anonymous list → PUBLISHED base queryset

    view = CaseViewSet()
    view.action = "list"
    view.request = request
    view.format_kwarg = None
    view.get_queryset()  # builds the queryset; the tag branch fires .filter()

    assert captured.get("tags__contains") == ["procurement"], (
        "postgres tag filter must use tags__contains=[tag]; "
        f"captured={captured!r}"
    )


# ============================================================================
# Property 16: Published cases display complete data
# ============================================================================


@pytest.mark.django_db
@settings(max_examples=20, deadline=800)
@given(case_data=complete_case_data())
def test_published_cases_display_complete_data(case_data):
    """
    Feature: accountability-platform-core, Property 16: Published cases display complete data

    For any published case retrieved via the public API, all associated evidence
    (material references) and timeline entries should be included.
    Validates: Requirements 6.3
    """
    # Create a case with complete data
    case = create_case_with_entities(**case_data)

    # Add a material reference (evidence)
    CaseMaterialReference.objects.create(
        case=case,
        material_iri=VALID_MATERIAL_IRI,
        additional_details="Evidence description",
    )

    # Publish the case
    case.state = CaseState.PUBLISHED
    case.save()

    # Retrieve via API
    client = APIClient()
    response = client.get(f"/api/cases/{case.slug}/")

    assert response.status_code == 200

    returned_case = response.data

    # Verify all core fields are present
    assert "slug" in returned_case, "Response should include slug"
    assert "title" in returned_case, "Response should include title"
    assert "description" in returned_case, "Response should include description"
    assert "case_type" in returned_case, "Response should include case_type"
    assert "entities" in returned_case, "Response should include entities"
    assert "key_allegations" in returned_case, "Response should include key_allegations"

    # Verify timeline is included
    assert "timeline" in returned_case, "Response should include timeline"
    if case.timeline:
        assert len(returned_case["timeline"]) == len(
            case.timeline
        ), "All timeline entries should be included"

    # Verify evidence is included
    assert "evidence" in returned_case, "Response should include evidence"
    assert len(returned_case["evidence"]) == case.material_references.count(), (
        "All evidence entries should be included"
    )

    # Verify evidence structure (material references)
    for evidence_item in returned_case["evidence"]:
        assert "material_iri" in evidence_item, "Evidence should include material_iri"
        assert (
            "additional_details" in evidence_item
        ), "Evidence should include additional_details"
        # Detail endpoint includes nested resolved material details
        assert "material" in evidence_item, "Detail endpoint should include material"

    # Verify tags are included
    assert "tags" in returned_case, "Response should include tags"
    if case.tags:
        assert len(returned_case["tags"]) == len(
            case.tags
        ), "All tags should be included"


@pytest.mark.django_db
@settings(max_examples=20, deadline=800)
@given(case_data=complete_case_data())
def test_published_cases_include_all_entity_fields(case_data):
    """
    Feature: accountability-platform-core, Property 16: Published cases display complete data

    For any published case, entity relationships should be included in the API response.
    Validates: Requirements 6.3
    """
    # Create and publish a case
    case = create_case_with_entities(**case_data)
    case.state = CaseState.PUBLISHED
    case.save()

    # Retrieve via API
    client = APIClient()
    response = client.get(f"/api/cases/{case.slug}/")

    assert response.status_code == 200

    returned_case = response.data

    # Verify unified entity field is present
    assert "entities" in returned_case, "Response should include entities"

    # A verdict outcome is meaningful only for the ACCUSED role — that is what
    # lets the SPA badge an acquitted defendant instead of rendering "accused".
    # Every other role (alleged/related/location) carries outcome=None.
    for entity in returned_case["entities"]:
        assert "outcome" in entity, "Entity should expose an outcome field"
        if entity["type"] == "accused":
            assert entity["outcome"] in {
                "charged",
                "convicted",
                "acquitted",
                "abated",
            }, "An accused entity must carry a verdict outcome"
        else:
            assert (
                entity["outcome"] is None
            ), "A non-accused entity must not carry a verdict outcome"

    # Verify entity lists are present and have correct structure
    alleged_in_response = [
        e for e in returned_case["entities"] if e["type"] == "alleged"
    ]
    alleged_in_db = case.entity_relationships.filter(
        relationship_type="alleged"
    ).count()

    # Verify entity objects have required fields
    assert (
        len(alleged_in_response) == alleged_in_db
    ), "alleged entities count should match"

    for entity in alleged_in_response:
        assert "id" in entity, "Entity should have id field"
        assert (
            "nes_id" in entity or "display_name" in entity
        ), "Entity should have nes_id or display_name"

    related_in_response = [
        e for e in returned_case["entities"] if e["type"] == "related"
    ]
    related_in_db = case.entity_relationships.filter(
        relationship_type="related"
    ).count()
    if related_in_db > 0:
        assert (
            len(related_in_response) == related_in_db
        ), "related entities count should match"


# ============================================================================
# Edge Cases and Additional Tests
# ============================================================================


@pytest.mark.django_db
def test_api_returns_empty_list_when_no_published_cases():
    """
    Edge case: API should return empty list when no published cases exist.
    Validates: Requirements 6.1, 8.3
    """
    # Create only draft cases
    create_case_with_entities(
        title="Draft Case",
        alleged_entities=["https://jawafdehi.org/entity/person/test"],
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
    )

    # Make API request
    client = APIClient()
    response = client.get("/api/cases/")

    assert response.status_code == 200
    assert (
        len(response.data.get("results", [])) == 0
    ), "API should return empty list when no published cases exist"


@pytest.mark.django_db
def test_api_does_not_expose_contributors():
    """
    Edge case: API should not expose contributors field (internal only).
    Validates: Design document - contributors not exposed in API
    """
    # Create and publish a case
    case = create_case_with_entities(
        title="Test Case",
        alleged_entities=["https://jawafdehi.org/entity/person/test"],
        key_allegations=["Test allegation"],
        case_type=CaseType.CORRUPTION,
        description="Test description",
        state=CaseState.PUBLISHED,
    )

    # Make API request
    client = APIClient()
    response = client.get(f"/api/cases/{case.slug}/")

    assert response.status_code == 200

    # Contributors should NOT be in response
    assert (
        "contributors" not in response.data
    ), "API should not expose contributors field"


@pytest.mark.django_db
def test_api_exposes_state_field():
    """
    Edge case: API should always expose state field to indicate case status.
    Validates: Design document - state field shows PUBLISHED or IN_REVIEW
    """
    # Create and publish a case
    case = create_case_with_entities(
        title="Test Case",
        alleged_entities=["https://jawafdehi.org/entity/person/test"],
        key_allegations=["Test allegation"],
        case_type=CaseType.CORRUPTION,
        description="Test description",
        state=CaseState.PUBLISHED,
    )

    # Make API request
    client = APIClient()
    response = client.get(f"/api/cases/{case.slug}/")

    assert response.status_code == 200

    # State should always be in response
    assert "state" in response.data, "API should always expose state field"
    assert response.data["state"] == CaseState.PUBLISHED


@pytest.mark.django_db
@settings(max_examples=10, deadline=800)
@given(case_data=complete_case_data())
def test_public_api_hides_in_review_case_from_retrieve(case_data):
    """
    Feature: IN_REVIEW cases are casework and NOT publicly accessible.

    Under the new role model (public = readonly EXCEPT no casework), an IN_REVIEW
    case is casework. The retrieve (detail) endpoint must return 404 to anonymous
    callers, and IN_REVIEW cases must NOT appear in the public list endpoint.

    Validates: in-review = casework = not public
    """
    # Create an IN_REVIEW case
    case = create_case_with_entities(**case_data)
    case.state = CaseState.IN_REVIEW
    case.save()

    client = APIClient()

    # Test 1: Detail endpoint must hide IN_REVIEW cases from the public (404)
    detail_response = client.get(f"/api/cases/{case.slug}/")
    assert (
        detail_response.status_code == 404
    ), "IN_REVIEW case should NOT be publicly accessible via detail endpoint"

    # Test 2: List endpoint should NOT show IN_REVIEW cases
    list_response = client.get("/api/cases/")
    assert list_response.status_code == 200

    case_ids_in_list = [c.get("slug") for c in list_response.data.get("results", [])]

    # IN_REVIEW cases should NOT appear in list
    assert (
        case.slug not in case_ids_in_list
    ), "IN_REVIEW case should NOT appear in list endpoint"


