"""
Integration tests for flexible case lookup feature.

Tests the CaseViewSet retrieve endpoint's ability to accept both numeric IDs
and slugs, with proper deprecation header handling.
"""

import pytest
from rest_framework.test import APIClient

from cases.models import CaseState, CaseType
from tests.conftest import create_case_with_entities


@pytest.mark.django_db
class TestFlexibleCaseLookup:
    """Integration tests for flexible case lookup by ID or slug."""

    def test_lookup_by_numeric_id_returns_deprecation_header(self):
        """
        GET /api/cases/{id}/ on a published case should return 200 with Deprecation: true header.
        """
        # Create a published case
        case = create_case_with_entities(
            title="Test Case for ID Lookup",
            alleged_entities=["entity:person/test-person"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.PUBLISHED,
            slug="test-case-id-lookup",
        )

        # Make request using numeric ID
        client = APIClient()
        response = client.get(f"/api/cases/{case.id}/")

        # Verify response
        assert response.status_code == 200, "Should return 200 for published case"
        assert "Deprecation" in response, "Response should include Deprecation header"
        assert response["Deprecation"] == "true", "Deprecation header should be 'true'"
        assert response.data["case_id"] == case.case_id, "Should return correct case"

    def test_lookup_by_slug_no_deprecation_header(self):
        """
        GET /api/cases/{slug}/ on a published case should return 200 without Deprecation header.
        """
        # Create a published case
        case = create_case_with_entities(
            title="Test Case for Slug Lookup",
            alleged_entities=["entity:person/test-person"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.PUBLISHED,
            slug="test-case-slug-lookup",
        )

        # Make request using slug
        client = APIClient()
        response = client.get(f"/api/cases/{case.slug}/")

        # Verify response
        assert response.status_code == 200, "Should return 200 for published case"
        assert (
            "Deprecation" not in response
        ), "Response should NOT include Deprecation header"
        assert response.data["case_id"] == case.case_id, "Should return correct case"
