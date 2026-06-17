"""Contract tests for the unified archive search endpoint."""

from datetime import timedelta

import pytest
from django.db import connection
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    DocumentSource,
    JawafEntity,
    RelationshipType,
    SourceType,
)


@pytest.fixture
def archive_records():
    person = JawafEntity.objects.create(
        nes_id="entity:person/kp-sharma-oli",
        display_name="K.P. Sharma Oli",
    )
    organization = JawafEntity.objects.create(
        nes_id="entity:organization/nepal-government",
        display_name="नेपाल सरकार",
    )
    location = JawafEntity.objects.create(
        nes_id="entity:location/kathmandu",
        display_name="काठमाडौं",
    )
    source = DocumentSource.objects.create(
        source_id="source:ciaa:procurement",
        title="CIAA procurement filing",
        description="Official procurement investigation filing.",
        source_type=SourceType.CIAA_PRESS_RELEASE,
    )
    published_case = Case.objects.create(
        case_id="case-procurement",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        title="KP Sharma Oli procurement case",
        thumbnail_url="https://example.com/procurement-thumbnail.jpg",
        short_description="Procurement accountability record.",
        description="A public procurement investigation.",
        key_allegations=["Irregular procurement decision"],
        tags=["procurement"],
        evidence=[{"source_id": source.source_id, "description": "Official filing"}],
        court_cases=["special:081-CR-0001"],
    )
    CaseEntityRelationship.objects.create(
        case=published_case,
        entity=person,
        relationship_type=RelationshipType.ACCUSED,
        notes="Named in irregular procurement allegation.",
    )
    CaseEntityRelationship.objects.create(
        case=published_case,
        entity=organization,
        relationship_type=RelationshipType.RELATED,
    )
    CaseEntityRelationship.objects.create(
        case=published_case,
        entity=location,
        relationship_type=RelationshipType.LOCATION,
    )
    source.related_entities.add(person)

    draft_case = Case.objects.create(
        case_id="case-private-draft",
        case_type=CaseType.CORRUPTION,
        state=CaseState.DRAFT,
        title="Private draft procurement note",
        description="Not ready for publication.",
    )
    return {
        "person": person,
        "organization": organization,
        "location": location,
        "source": source,
        "published_case": published_case,
        "draft_case": draft_case,
    }


@pytest.mark.django_db
def test_search_returns_one_mixed_normalized_result_list(archive_records):
    response = APIClient().get("/api/search/", {"q": "K.P. Sharma Oli"})

    assert response.status_code == 200
    assert response.data["query"] == "K.P. Sharma Oli"
    assert response.data["count"] == 3
    assert response.data["counts"] == {
        "all": 3,
        "cases": 1,
        "entities": 1,
        "documents": 1,
    }
    results_by_type = {
        result["result_type"]: result for result in response.data["results"]
    }
    assert set(results_by_type) == {"case", "entity", "document"}
    assert results_by_type["case"]["url"] == (
        f"/case/{archive_records['published_case'].slug}"
    )
    assert results_by_type["case"]["image_url"] == (
        "https://example.com/procurement-thumbnail.jpg"
    )
    assert results_by_type["entity"]["url"] == (
        f"/entity/{archive_records['person'].id}"
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("params", "expected_types"),
    [
        ({"type": "case"}, {"case"}),
        ({"type": "entity"}, {"entity"}),
        ({"type": "document"}, {"document"}),
        ({"role": "accused"}, {"case", "entity", "document"}),
        ({"case_type": "CORRUPTION"}, {"case", "entity", "document"}),
    ],
)
def test_search_supports_type_and_relationship_filters(
    archive_records, params, expected_types
):
    response = APIClient().get("/api/search/", params)

    assert response.status_code == 200
    assert {result["result_type"] for result in response.data["results"]} == (
        expected_types
    )


@pytest.mark.django_db
def test_search_supports_repeatable_refinement_filters(archive_records):
    response = APIClient().get(
        "/api/search/",
        [
            ("type", "case"),
            ("type", "entity"),
            ("entity_type", "person"),
            ("entity_type", "organization"),
            ("role", "accused"),
            ("role", "related"),
            ("case_type", "CORRUPTION"),
            ("tags", "procurement"),
        ],
    )

    assert response.status_code == 200
    assert {result["result_type"] for result in response.data["results"]} == {
        "case",
        "entity",
    }
    entity_ids = {
        result["id"]
        for result in response.data["results"]
        if result["result_type"] == "entity"
    }
    assert entity_ids == {
        archive_records["person"].id,
        archive_records["organization"].id,
    }


@pytest.mark.django_db
@override_settings(ARCHIVE_SEARCH_USE_POSTGRES=True)
def test_role_and_entity_type_filters_match_same_relationship(archive_records):
    archive_records["source"].related_entities.add(archive_records["organization"])

    response = APIClient().get(
        "/api/search/", {"role": "accused", "entity_type": "organization"}
    )

    assert response.status_code == 200
    assert response.data["results"] == []
    assert response.data["count"] == 0


@pytest.mark.django_db
def test_entity_type_refines_mixed_results(archive_records):
    response = APIClient().get("/api/search/", {"entity_type": "organization"})

    assert response.status_code == 200
    assert {result["result_type"] for result in response.data["results"]} == {
        "case",
        "entity",
    }
    entity_results = [
        result
        for result in response.data["results"]
        if result["result_type"] == "entity"
    ]
    assert [result["id"] for result in entity_results] == [
        archive_records["organization"].id
    ]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "query",
    [
        "KP Sharma",
        "Irregular procurement",
        "procurement",
        "entity:person/kp-sharma-oli",
    ],
)
def test_search_matches_case_and_entity_fields(archive_records, query):
    response = APIClient().get("/api/search/", {"q": query})

    assert response.status_code == 200
    result_types = {result["result_type"] for result in response.data["results"]}
    assert "case" in result_types
    assert "entity" in result_types


@pytest.mark.django_db
def test_search_matches_document_fields_without_case_text_match(archive_records):
    response = APIClient().get("/api/search/", {"q": "CIAA filing"})

    assert response.status_code == 200
    assert [result["result_type"] for result in response.data["results"]] == [
        "document"
    ]
    assert response.data["results"][0]["id"] == archive_records["source"].id


@pytest.mark.django_db
def test_search_matches_nepali_text(archive_records):
    response = APIClient().get("/api/search/", {"q": "नेपाल सरकार"})

    assert response.status_code == 200
    assert {result["result_type"] for result in response.data["results"]} == {
        "case",
        "entity",
    }
    assert archive_records["organization"].id in {
        result["id"]
        for result in response.data["results"]
        if result["result_type"] == "entity"
    }


@pytest.mark.django_db
@override_settings(ARCHIVE_SEARCH_USE_POSTGRES=True)
def test_search_tolerates_minor_title_typos(archive_records):
    if connection.vendor != "postgresql":
        pytest.skip("Typo tolerance is provided by PostgreSQL pg_trgm")

    response = APIClient().get("/api/search/", {"q": "procuremnt"})

    assert response.status_code == 200
    assert {result["result_type"] for result in response.data["results"]} >= {
        "case",
        "document",
    }


@pytest.mark.django_db
def test_public_search_does_not_expose_draft_cases(archive_records):
    response = APIClient().get("/api/search/", {"q": "Private draft"})

    assert response.status_code == 200
    assert response.data["results"] == []
    assert response.data["count"] == 0


@pytest.mark.django_db
def test_public_search_does_not_expose_in_review_only_entities(archive_records):
    private_entity = JawafEntity.objects.create(
        nes_id="entity:person/private-review-only",
        display_name="Private Review Only",
    )
    private_source = DocumentSource.objects.create(
        source_id="source:ciaa:private-review",
        title="In-review source",
        description="Visible source without a published entity association.",
    )
    private_source.related_entities.add(private_entity)
    Case.objects.create(
        case_id="case-private-review",
        case_type=CaseType.CORRUPTION,
        state=CaseState.IN_REVIEW,
        title="Private review case",
        evidence=[{"source_id": private_source.source_id, "description": "Review"}],
    )

    response = APIClient().get("/api/search/", {"q": "In-review source"})

    assert response.status_code == 200
    assert response.data["results"] == []


@pytest.mark.django_db
def test_facets_are_calculated_before_pagination(archive_records):
    response = APIClient().get("/api/search/", {"page": 1, "page_size": 2})

    assert response.status_code == 200
    assert response.data["count"] == 5
    assert len(response.data["results"]) == 2
    type_facets = {
        item["name"]: item["count"] for item in response.data["facets"]["type"]
    }
    assert type_facets == {
        "case": 1,
        "entity": 3,
        "document": 1,
    }
    entity_type_facets = {
        item["name"]: item["count"] for item in response.data["facets"]["entity_type"]
    }
    assert entity_type_facets == {
        "person": 1,
        "organization": 1,
        "location": 1,
        "unknown": 0,
    }
    role_facets = {
        item["name"]: item["count"] for item in response.data["facets"]["role"]
    }
    assert role_facets["accused"] == 1
    assert role_facets["related"] == 1
    assert role_facets["location"] == 1
    assert response.data["facets"]["tags"] == [
        {"name": "procurement", "display_name": "Procurement", "count": 1}
    ]


@pytest.mark.django_db
@override_settings(ARCHIVE_SEARCH_USE_POSTGRES=True)
def test_postgres_sparse_facets_use_zero_count_buckets(archive_records):
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL-specific facet regression")

    response = APIClient().get("/api/search/", {"q": "no matching archive record"})

    assert response.status_code == 200
    assert response.data["count"] == 0
    entity_type_facets = {
        item["name"]: item["count"] for item in response.data["facets"]["entity_type"]
    }
    role_facets = {
        item["name"]: item["count"] for item in response.data["facets"]["role"]
    }
    case_type_facets = {
        item["name"]: item["count"] for item in response.data["facets"]["case_type"]
    }
    assert entity_type_facets == {
        "person": 0,
        "organization": 0,
        "location": 0,
        "unknown": 0,
    }
    assert role_facets[RelationshipType.ACCUSED] == 0
    assert role_facets[RelationshipType.WITNESS] == 0
    assert case_type_facets[CaseType.CORRUPTION] == 0


@pytest.mark.django_db
def test_case_type_facet_exposes_stable_name_and_choice_label(archive_records):
    """The case_type facet keys every entry by its stable CaseType value and
    carries the CaseType choice label as ``display_name``.

    The choice label is intentionally bilingual ("English (नेपाली)") so the
    Django admin shows both languages. The frontend therefore localizes the
    archive-search filter from the stable ``name`` rather than rendering this
    ``display_name`` verbatim; this test pins that contract so a label tweak
    can't silently change the facet keys the frontend depends on.
    """
    response = APIClient().get("/api/search/")

    assert response.status_code == 200
    case_type_facets = {
        item["name"]: item["display_name"]
        for item in response.data["facets"]["case_type"]
    }
    # Every CaseType value is present and keyed by its stable value...
    assert set(case_type_facets) == set(CaseType.values)
    # ...and the display_name is the (bilingual) choice label, not the value.
    assert case_type_facets[CaseType.CORRUPTION] == CaseType.CORRUPTION.label
    assert "भ्रष्टाचार" in case_type_facets[CaseType.CORRUPTION]
    assert case_type_facets[CaseType.BRIBERY] == CaseType.BRIBERY.label


@pytest.mark.django_db
def test_newest_sort_is_deterministic(archive_records):
    other_case = Case.objects.create(
        case_id="case-newer",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        title="Newest published record",
    )
    Case.objects.filter(pk=other_case.pk).update(
        created_at=timezone.now() + timedelta(days=1)
    )

    response = APIClient().get("/api/search/", {"type": "case", "sort": "newest"})

    assert response.status_code == 200
    assert response.data["results"][0]["id"] == other_case.id


@pytest.mark.django_db
def test_page_size_is_capped_and_invalid_params_are_rejected(archive_records):
    capped_response = APIClient().get("/api/search/", {"page_size": 500})
    invalid_response = APIClient().get("/api/search/", {"type": "invalid"})

    assert capped_response.status_code == 200
    assert capped_response.data["page_size"] == 50
    assert invalid_response.status_code == 400
