"""
Contract tests: validate API responses match documented schemas using Pydantic.

Each test creates fixtures with real data, calls the endpoint, and validates
the response deserializes cleanly into a Pydantic model that mirrors the shape
the TypeScript client will expect after `openapi-typescript` generation.

A failure here means the API contract has drifted from what the frontend client
will generate -- either the serializer changed, or the TypeScript types need
regeneration.
"""

from __future__ import annotations

from typing import Any

import pytest
from django.test import Client
from pydantic import BaseModel, ConfigDict

from cases.models import CaseState, CaseType
from tests.conftest import (
    create_case_with_entities,
    create_document_source_with_entities,
)


class SimplifiedEntity(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: int
    nes_id: str | None = None
    display_name: str | None = None
    type: str
    notes: str | None = None


class CaseListItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: int
    case_id: str
    slug: str
    case_type: str
    state: str
    title: str
    short_description: str | None = None
    thumbnail_url: str | None = None
    banner_url: str | None = None
    case_start_date: str | None = None
    case_end_date: str | None = None
    entities: list[SimplifiedEntity]
    tags: list[str]
    description: str | None = None
    key_allegations: list[str] | None = None
    timeline: list[dict[str, Any]] | None = None
    evidence: list[dict[str, Any]] | None = None
    notes: str | None = None
    court_cases: list[str] | None = None
    missing_details: str | None = None
    bigo: str | None = None
    versionInfo: dict[str, Any] | None = None
    created_at: str
    updated_at: str


class PaginatedCases(BaseModel):
    count: int
    next: str | None = None
    previous: str | None = None
    results: list[CaseListItem]


class CaseEvidenceEntry(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    source_id: str | None = None
    description: str | None = None
    source: dict[str, Any] | None = None


class CaseDetail(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: int
    case_id: str
    slug: str
    case_type: str
    state: str
    title: str
    short_description: str | None = None
    thumbnail_url: str | None = None
    banner_url: str | None = None
    case_start_date: str | None = None
    case_end_date: str | None = None
    entities: list[SimplifiedEntity]
    tags: list[str]
    description: str | None = None
    key_allegations: list[str] | None = None
    timeline: list[dict[str, Any]] | None = None
    evidence: list[CaseEvidenceEntry] | None = None
    notes: str | None = None
    court_cases: list[str] | None = None
    missing_details: str | None = None
    bigo: str | None = None
    versionInfo: dict[str, Any] | None = None
    created_at: str
    updated_at: str


class DocumentSourceItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: int
    source_id: str
    title: str
    description: str | None = None
    source_type: str | None = None
    url: list[str] | None = None
    publication_date: str | None = None
    created_at: str
    updated_at: str


class PaginatedSources(BaseModel):
    count: int
    next: str | None = None
    previous: str | None = None
    results: list[DocumentSourceItem]


class JawafEntityItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    id: int
    nes_id: str | None = None
    display_name: str | None = None
    related_cases: list[dict[str, Any]]


class PaginatedEntities(BaseModel):
    count: int
    next: str | None = None
    previous: str | None = None
    results: list[JawafEntityItem]


class StatisticsResponse(BaseModel):
    published_cases: int
    cases_under_investigation: int
    cases_closed: int
    entities_tracked: int
    last_updated: str


@pytest.fixture
def published_case(db):
    case = create_case_with_entities(
        case_id="contract-test-case-001",
        case_type=CaseType.CORRUPTION,
        state=CaseState.PUBLISHED,
        title="Contract Test Case",
        short_description="A case for contract testing",
        description="Full description for contract testing",
        alleged_entities=["entity:person/contract-test-person"],
        related_entities=["entity:organization/contract-test-org"],
        locations=["entity:location/kathmandu"],
        tags=["contract-test", "corruption"],
        key_allegations=["Allegation one", "Allegation two"],
        timeline=[
            {
                "date": "2024-06-01",
                "title": "Incident",
                "description": "The incident",
            }
        ],
        evidence=[{"source_id": "source:contract:001", "description": "Test evidence"}],
        versionInfo={
            "action": "published",
            "datetime": "2024-06-15T10:00:00Z",
        },
    )
    return case


@pytest.fixture
def document_source(published_case):
    source = create_document_source_with_entities(
        source_id="source:contract:001",
        title="Contract Test Source",
        description="A source for contract testing",
        source_type="MEDIA_NEWS",
        publication_date="2024-06-01",
        url=["https://example.com/contract-test"],
        related_entity_ids=["entity:person/contract-test-person"],
    )
    published_case.evidence = [
        {"source_id": source.source_id, "description": "Test evidence"}
    ]
    published_case.save()
    return source


class TestCaseListContract:

    def test_response_parses_into_pydantic_model(self, published_case):
        client = Client()
        response = client.get("/api/cases/")
        assert response.status_code == 200

        data = response.json()
        parsed = PaginatedCases.model_validate(data)
        assert parsed.count >= 1
        assert len(parsed.results) >= 1

        item = parsed.results[0]
        assert item.case_id == "contract-test-case-001"
        assert item.case_type == "CORRUPTION"
        assert item.state == "PUBLISHED"
        assert len(item.entities) >= 1
        assert "contract-test" in item.tags

    def test_entity_shape_in_list_response(self, published_case):
        client = Client()
        response = client.get("/api/cases/")
        data = response.json()
        parsed = PaginatedCases.model_validate(data)
        entity = parsed.results[0].entities[0]
        assert isinstance(entity.id, int)
        assert entity.nes_id is not None
        assert entity.type in ("alleged", "accused", "related", "witness", "opposition", "victim", "location")
        assert entity.notes is None or isinstance(entity.notes, str)

    def test_pagination_fields_are_correct_types(self, published_case):
        client = Client()
        response = client.get("/api/cases/")
        data = response.json()
        parsed = PaginatedCases.model_validate(data)
        assert isinstance(parsed.count, int)
        assert isinstance(parsed.results, list)


class TestCaseDetailContract:

    def test_response_parses_into_pydantic_model(self, published_case, document_source):
        client = Client()
        response = client.get(f"/api/cases/{published_case.slug}/")
        assert response.status_code == 200

        data = response.json()
        parsed = CaseDetail.model_validate(data)
        assert parsed.case_id == "contract-test-case-001"
        assert parsed.slug == published_case.slug
        assert parsed.title == "Contract Test Case"

    def test_evidence_includes_enriched_source(self, published_case, document_source):
        client = Client()
        response = client.get(f"/api/cases/{published_case.slug}/")
        data = response.json()
        parsed = CaseDetail.model_validate(data)
        assert parsed.evidence is not None
        for entry in parsed.evidence:
            assert entry.source_id is not None
            if entry.source is not None:
                assert "title" in entry.source
                assert "source_type" in entry.source
                assert "url" in entry.source

    def test_detail_has_all_required_fields(self, published_case):
        client = Client()
        response = client.get(f"/api/cases/{published_case.slug}/")
        data = response.json()
        parsed = CaseDetail.model_validate(data)
        assert parsed.case_type in ("CORRUPTION", "PROMISES")
        assert parsed.state in ("PUBLISHED", "IN_REVIEW", "DRAFT", "CLOSED")
        assert isinstance(parsed.tags, list)
        assert isinstance(parsed.entities, list)
        assert parsed.created_at is not None
        assert parsed.updated_at is not None


class TestDocumentSourceContract:

    def test_response_parses_into_pydantic_model(self, document_source):
        client = Client()
        response = client.get("/api/sources/")
        assert response.status_code == 200

        data = response.json()
        parsed = PaginatedSources.model_validate(data)
        assert parsed.count >= 1

        item = parsed.results[0]
        assert item.source_id == "source:contract:001"
        assert item.title == "Contract Test Source"
        assert item.source_type == "MEDIA_NEWS"
        assert isinstance(item.url, list)


class TestEntityContract:

    def test_response_structure_is_valid(self, published_case):
        client = Client()
        response = client.get("/api/entities/")
        assert response.status_code == 200

        data = response.json()
        parsed = PaginatedEntities.model_validate(data)
        assert isinstance(parsed.count, int)
        assert isinstance(parsed.results, list)

    def test_item_shape_if_entities_exist(self, published_case):
        client = Client()
        response = client.get("/api/entities/")
        data = response.json()
        parsed = PaginatedEntities.model_validate(data)
        if parsed.count > 0:
            item = parsed.results[0]
            assert isinstance(item.id, int)
            assert isinstance(item.related_cases, list)
            assert item.nes_id is not None or item.display_name is not None


class TestStatisticsContract:

    def test_response_parses_into_pydantic_model(self, published_case):
        client = Client()
        response = client.get("/api/statistics/")
        assert response.status_code == 200

        data = response.json()
        parsed = StatisticsResponse.model_validate(data)

        assert isinstance(parsed.published_cases, int)
        assert isinstance(parsed.cases_under_investigation, int)
        assert isinstance(parsed.cases_closed, int)
        assert isinstance(parsed.entities_tracked, int)
        assert isinstance(parsed.last_updated, str)

    def test_statistics_reflect_test_data(self, published_case):
        client = Client()
        response = client.get("/api/statistics/")
        data = response.json()
        parsed = StatisticsResponse.model_validate(data)
        assert parsed.published_cases >= 1
        assert parsed.entities_tracked >= 1


class TestOpenApiSchemaContract:

    def test_schema_components_match_serializer_fields(self, published_case):
        client = Client()
        response = client.get("/api/schema/")
        assert response.status_code == 200

        import yaml
        schema = yaml.safe_load(response.content)

        case_schema = schema["components"]["schemas"]["Case"]
        expected = {
            "id", "case_id", "slug", "case_type", "state", "title",
            "short_description", "entities", "tags", "description",
            "key_allegations", "timeline", "evidence", "notes",
            "court_cases", "missing_details", "bigo", "versionInfo",
            "created_at", "updated_at",
        }
        actual = set(case_schema.get("properties", {}).keys())
        missing = expected - actual
        assert not missing, f"Fields missing from Case schema: {missing}"

        detail_schema = schema["components"]["schemas"]["CaseDetail"]
        detail_expected = expected | {"slug"}
        detail_actual = set(detail_schema.get("properties", {}).keys())
        detail_missing = detail_expected - detail_actual
        assert not detail_missing, f"Fields missing from CaseDetail schema: {detail_missing}"


class TestSchemaConformance:

    def test_case_list_response_conforms_to_schema(self, published_case):
        client = Client()
        schema_resp = client.get("/api/schema/")
        import yaml
        schema = yaml.safe_load(schema_resp.content)

        api_resp = client.get("/api/cases/")
        assert api_resp.status_code == 200
        api_data = api_resp.json()

        case_schema = schema["components"]["schemas"]["Case"]
        schema_props = set(case_schema.get("properties", {}).keys())

        for result in api_data["results"][:1]:
            for prop in result:
                assert prop in schema_props, (
                    f"Field '{prop}' in API response not documented in Case schema"
                )
