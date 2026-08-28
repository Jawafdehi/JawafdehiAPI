"""
Tests for OpenAPI documentation endpoints.

Validates that the API documentation is properly configured and accessible.
"""

import pytest
from django.test import Client
from django.urls import reverse


@pytest.mark.django_db
class TestOpenAPIDocumentation:
    """Test OpenAPI schema and documentation endpoints."""

    def test_schema_endpoint_accessible(self):
        """Test that the OpenAPI schema endpoint is accessible."""
        client = Client()
        response = client.get(reverse("schema"))

        assert response.status_code == 200
        assert "application/vnd.oai.openapi" in response["Content-Type"]

    def test_schema_contains_api_info(self):
        """Test that the schema contains proper API information."""
        client = Client()
        response = client.get(reverse("schema"))

        # Parse the YAML response
        import yaml

        schema = yaml.safe_load(response.content)

        # Verify basic structure
        assert "openapi" in schema
        assert "info" in schema
        assert "paths" in schema
        assert "components" in schema

        # Verify API info
        assert schema["info"]["title"] == "Jawafdehi Public Accountability API"
        assert schema["info"]["version"] == "1.0.0"
        assert "description" in schema["info"]

    def test_schema_contains_case_endpoints(self):
        """Test that the schema documents case endpoints."""
        client = Client()
        response = client.get(reverse("schema"))

        import yaml

        schema = yaml.safe_load(response.content)

        # Verify case endpoints are documented
        assert "/api/cases/" in schema["paths"]
        assert "/api/cases/{slug}/" in schema["paths"]
        assert "post" in schema["paths"]["/api/cases/"]

        # Verify list endpoint has proper documentation
        cases_list = schema["paths"]["/api/cases/"]["get"]
        assert "summary" in cases_list
        assert "description" in cases_list
        assert "parameters" in cases_list

        # Verify create endpoint has proper documentation
        cases_create = schema["paths"]["/api/cases/"]["post"]
        assert "summary" in cases_create
        assert "description" in cases_create
        assert "requestBody" in cases_create

        # Verify parameters are documented
        param_names = [p["name"] for p in cases_list["parameters"]]
        assert "case_type" in param_names
        assert "tags" in param_names
        assert "search" in param_names
        assert "page" in param_names

    def test_schema_contains_unified_search_endpoint(self):
        """Test that the schema documents the unified platform search.

        Updated for the OpenSearch cutover: search is now the platform-wide
        ``search`` app at ``/api/search/`` (bilingual, all four domains), not the
        old cases-scoped archive search.
        """
        client = Client()
        response = client.get(reverse("schema"))

        import yaml

        schema = yaml.safe_load(response.content)

        assert "/api/search/" in schema["paths"]
        search = schema["paths"]["/api/search/"]["get"]
        assert search["summary"] == "Unified platform search"
        assert "search" in search["tags"]
        parameter_names = [parameter["name"] for parameter in search["parameters"]]
        # The unified-search params: q (optional → empty = browse), type/lang
        # filters, sort mode, exact-match refine facets (entity_type/case_type/
        # tags/status, plus the court-case-scoped court/court_type/district/
        # province), the RANGE bounds (bigo_min/bigo_max and date_from/date_to),
        # the facet-value search (facet_q), offset paging (page/page_size), and
        # the deep-paging cursor (search_after). ``status`` is the coarse
        # case-lifecycle refine facet; ``court`` is one specific court's
        # identifier and ``court_type`` its tier.
        assert set(parameter_names) == {
            "q",
            "type",
            "lang",
            "sort",
            "entity_type",
            "case_type",
            "tags",
            "status",
            "court",
            "court_type",
            "district",
            "province",
            "bigo_min",
            "bigo_max",
            "date_from",
            "date_to",
            "facet_q",
            "page",
            "page_size",
            "cursor",
        }

    def test_schema_contains_component_schemas(self):
        """Test that the schema contains component definitions."""
        client = Client()
        response = client.get(reverse("schema"))

        import yaml

        schema = yaml.safe_load(response.content)

        # Verify component schemas exist
        assert "schemas" in schema["components"]
        assert "Case" in schema["components"]["schemas"]
        assert "CaseDetail" in schema["components"]["schemas"]

        # Verify Case schema has proper fields
        case_schema = schema["components"]["schemas"]["Case"]
        assert "properties" in case_schema
        assert "slug" in case_schema["properties"]
        assert "case_type" in case_schema["properties"]
        assert "title" in case_schema["properties"]
        assert "entities" in case_schema["properties"]
        assert "evidence" in case_schema["properties"]
        assert "timeline" in case_schema["properties"]

    def test_swagger_ui_accessible(self):
        """Test that the Swagger UI is accessible."""
        client = Client()
        response = client.get(reverse("swagger-ui"))

        assert response.status_code == 200
        assert "text/html" in response["Content-Type"]

    def test_schema_has_tags(self):
        """Test that the schema has proper tags for organization."""
        client = Client()
        response = client.get(reverse("schema"))

        import yaml

        schema = yaml.safe_load(response.content)

        # Verify endpoints are tagged
        cases_list = schema["paths"]["/api/cases/"]["get"]
        assert "tags" in cases_list
        assert "cases" in cases_list["tags"]

    def test_schema_documents_case_type_enum(self):
        """Test that the CaseType enum is properly documented."""
        client = Client()
        response = client.get(reverse("schema"))

        import yaml

        schema = yaml.safe_load(response.content)

        # Find CaseTypeEnum in components
        assert "CaseTypeEnum" in schema["components"]["schemas"]
        case_type_enum = schema["components"]["schemas"]["CaseTypeEnum"]

        # Verify enum values
        assert "enum" in case_type_enum
        assert "CORRUPTION" in case_type_enum["enum"]
        assert "CORRUPTION" in case_type_enum["enum"]
