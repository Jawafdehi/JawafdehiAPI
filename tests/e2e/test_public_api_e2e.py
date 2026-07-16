"""
End-to-End tests for public API workflows.

Feature: accountability-platform-core
Tests complete user workflows through the public API
Validates: Requirements 6.1, 6.2, 6.3, 8.1, 8.3
"""

import pytest
from rest_framework.test import APIClient

from cases.models import CaseMaterialReference, CaseState, CaseType
from tests.conftest import (
    create_case_with_entities,
    create_user_with_role,
)

VALID_MATERIAL_IRI = "https://jawafdehi.org/material/jawafdehi/20240115.ab12cd"


@pytest.mark.django_db
class TestPublicAPIWorkflows:
    """
    End-to-end tests for public API user workflows.

    These tests simulate complete user journeys through the API,
    testing the integration of multiple endpoints and features.
    """

    def setup_method(self):
        """Set up test data for each test."""
        self.client = APIClient()

        # Create test cases with different states and types
        self.published_corruption_case = create_case_with_entities(
            title="Corruption Case - Land Encroachment",
            alleged_entities=["https://jawafdehi.org/entity/person/test-official"],
            related_entities=["https://jawafdehi.org/entity/organization/test-ministry"],
            locations=["https://jawafdehi.org/entity/location/district/kathmandu"],
            key_allegations=[
                "Illegally acquired public land",
                "Failed to disclose assets",
            ],
            case_type=CaseType.CORRUPTION,
            description="A detailed description of the corruption case involving land encroachment.",
            tags=["land-encroachment", "public-land"],
            timeline=[
                {
                    "date": "2024-01-15",
                    "title": "Initial complaint filed",
                    "description": "Citizens filed complaint with authorities",
                },
                {
                    "date": "2024-02-20",
                    "title": "Investigation started",
                    "description": "Official investigation commenced",
                },
            ],
            state=CaseState.PUBLISHED,
        )

        # Add evidence (a material reference) to the case
        self.corruption_material_iri = VALID_MATERIAL_IRI
        CaseMaterialReference.objects.create(
            case=self.published_corruption_case,
            material_iri=self.corruption_material_iri,
            additional_details="This document proves the illegal land transfer",
        )

        # Create another published case with different type
        self.infrastructure_case = create_case_with_entities(
            title="Broken Promise - Infrastructure Project",
            alleged_entities=["https://jawafdehi.org/entity/person/test-politician"],
            key_allegations=["Failed to deliver promised infrastructure"],
            case_type=CaseType.CORRUPTION,
            description="Election promise to build hospital was not fulfilled.",
            tags=["infrastructure", "healthcare"],
            state=CaseState.PUBLISHED,
        )

        # Create a draft case (should not be visible)
        self.draft_case = create_case_with_entities(
            title="Draft Case - Should Not Appear",
            alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="This is a draft case",
            state=CaseState.DRAFT,
        )

        # Create a closed case (should not be visible)
        self.closed_case = create_case_with_entities(
            title="Closed Case - Should Not Appear",
            alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="This is a closed case",
            state=CaseState.CLOSED,
        )

    def test_browse_filter_search_view_workflow(self):
        """
        E2E Test: Complete user workflow from browsing to viewing details.

        Workflow:
        1. Browse all cases (list endpoint)
        2. Filter by case type
        3. Search for specific term
        4. View detailed case information

        Validates: Requirements 6.1, 6.2, 6.3, 8.1
        """
        # Step 1: Browse all published cases
        response = self.client.get("/api/cases/")
        assert response.status_code == 200, "Browse endpoint should return 200"

        results = response.data.get("results", [])
        assert (
            len(results) == 2
        ), "Should return 2 published cases (not draft or closed)"

        # Verify only published cases appear
        case_titles = [case["title"] for case in results]
        assert "Corruption Case - Land Encroachment" in case_titles
        assert "Broken Promise - Infrastructure Project" in case_titles
        assert "Draft Case - Should Not Appear" not in case_titles
        assert "Closed Case - Should Not Appear" not in case_titles

        # Step 2: Filter by case type (CORRUPTION)
        response = self.client.get("/api/cases/?case_type=CORRUPTION")
        assert response.status_code == 200, "Filter endpoint should return 200"

        results = response.data.get("results", [])
        assert len(results) == 2, "Should return 2 corruption cases (PROMISES removed)"
        for result in results:
            assert result["case_type"] == CaseType.CORRUPTION
        titles = [case["title"] for case in results]
        assert "Corruption Case - Land Encroachment" in titles
        assert "Broken Promise - Infrastructure Project" in titles

        # Step 3: Search for specific term
        response = self.client.get("/api/cases/?search=land")
        assert response.status_code == 200, "Search endpoint should return 200"

        results = response.data.get("results", [])
        assert len(results) >= 1, "Should find at least 1 case with 'land' in content"

        # Find the corruption case in results
        corruption_case_result = next(
            (case for case in results if "Land Encroachment" in case["title"]), None
        )
        assert (
            corruption_case_result is not None
        ), "Should find the land encroachment case"

        # Step 4: View detailed case information
        case_slug = corruption_case_result["slug"]
        response = self.client.get(f"/api/cases/{case_slug}/")
        assert response.status_code == 200, "Detail endpoint should return 200"

        case_detail = response.data

        # Verify complete data is present
        assert case_detail["title"] == "Corruption Case - Land Encroachment"
        assert case_detail["description"] is not None
        assert len(case_detail["key_allegations"]) == 2
        assert len(case_detail["timeline"]) == 2
        assert len(case_detail["evidence"]) == 1
        assert len(case_detail["tags"]) == 2

        # Verify evidence includes material reference information
        evidence = case_detail["evidence"][0]
        assert "material_iri" in evidence
        assert "additional_details" in evidence
        assert evidence["material_iri"] == self.corruption_material_iri
        # Detail endpoint enriches evidence with a nested resolved material object
        assert "material" in evidence
        assert "display_name" in evidence["material"]
        assert "material_type" in evidence["material"]
        assert "urls" in evidence["material"]

    def test_only_published_cases_accessible(self):
        """
        E2E Test: Verify that only published cases are accessible through the API.

        Tests:
        1. List endpoint only shows published cases
        2. Draft cases are not accessible via detail endpoint
        3. Closed cases are not accessible via detail endpoint
        4. In Review cases (casework) are NOT publicly accessible via detail or list

        Validates: Requirements 6.1, 8.3
        """
        # Test 1: List endpoint only shows published cases
        response = self.client.get("/api/cases/")
        assert response.status_code == 200

        results = response.data.get("results", [])
        case_ids = [case["slug"] for case in results]

        assert self.published_corruption_case.slug in case_ids

        assert self.draft_case.slug not in case_ids
        assert self.closed_case.slug not in case_ids

        # Test 2: Draft cases return 404 when accessed directly
        response = self.client.get(f"/api/cases/{self.draft_case.id}/")
        assert (
            response.status_code == 404
        ), "Draft cases should not be accessible via detail endpoint"

        # Test 3: Closed cases return 404 when accessed directly
        response = self.client.get(f"/api/cases/{self.closed_case.id}/")
        assert (
            response.status_code == 404
        ), "Closed cases should not be accessible via detail endpoint"

        # Test 4: Create an IN_REVIEW case and verify unlisted-but-slug-accessible
        in_review_case = create_case_with_entities(
            title="In Review Case",
            alleged_entities=["https://jawafdehi.org/entity/person/test-person"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="This is an in-review case",
            state=CaseState.IN_REVIEW,
        )

        # IN_REVIEW is UNLISTED but public by direct slug: an anonymous caller
        # with the exact slug retrieves it (200), it's just kept out of listings.
        response = self.client.get(f"/api/cases/{in_review_case.slug}/")
        assert (
            response.status_code == 200
        ), "IN_REVIEW must be retrievable by direct slug (unlisted, not hidden)"

        # IN_REVIEW cases should still not appear in the list endpoint (unlisted)
        response = self.client.get("/api/cases/")
        case_ids = [case["slug"] for case in response.data.get("results", [])]
        assert (
            in_review_case.slug not in case_ids
        ), "In Review cases should not appear in list"

    def test_notes_are_hidden_from_public_but_shown_to_casework(self):
        """
        E2E Test: internal ``notes`` are gated by casework role (BB-04).

        ``notes`` (case-level and per-entity relationship notes) are internal —
        the authoring UI labels them "not shown publicly". The public/anonymous
        response must carry the ``notes`` key (schema stability) but with an empty
        value; an authenticated casework role sees the real content.

        Workflow:
        1. Create a published case with case-level and per-entity notes.
        2. Retrieve anonymously -> notes gated to "" (no leak).
        3. Retrieve as a Caseworker -> real notes returned (editor round-trip).

        Validates: Requirements 6.3 (corrected: notes are casework-only).
        """
        internal_note = (
            "## Background\n\nThis case involves corruption at the ministry level."
        )
        entity_note = "Internal: suspected primary beneficiary."

        # Step 1: Create a published case with case-level + per-entity notes.
        case = create_case_with_entities(
            title="Case with Notes",
            alleged_entities=["https://jawafdehi.org/entity/person/test-official"],
            key_allegations=["Initial allegation"],
            case_type=CaseType.CORRUPTION,
            description="A case with markdown notes.",
            state=CaseState.PUBLISHED,
        )
        case.notes = internal_note
        case.save()
        rel = case.entity_relationships.first()
        rel.notes = entity_note
        rel.save()

        # Step 2: Anonymous retrieval must NOT leak notes.
        response = self.client.get(f"/api/cases/{case.slug}/")
        assert response.status_code == 200
        public_detail = response.data
        assert "notes" in public_detail, "notes key should remain for schema stability"
        assert public_detail["notes"] == "", "case notes must be hidden from the public"
        assert all(
            e["notes"] == "" for e in public_detail["entities"]
        ), "per-entity notes must be hidden from the public"

        # audit_history is still never exposed.
        assert "audit_history" not in public_detail

        # Step 3: A casework role (Caseworker) still sees the real notes so the
        # admin editor can reload and round-trip them.
        caseworker = create_user_with_role(
            "cw-notes", "cw-notes@example.com", "Caseworker"
        )
        casework_client = APIClient()
        casework_client.force_authenticate(user=caseworker)
        cw_response = casework_client.get(f"/api/cases/{case.slug}/")
        assert cw_response.status_code == 200
        cw_detail = cw_response.data
        assert cw_detail["notes"] == internal_note, "casework must see case notes"
        assert any(
            e["notes"] == entity_note for e in cw_detail["entities"]
        ), "casework must see per-entity notes"

    def test_filter_by_tags_workflow(self):
        """
        E2E Test: Filter cases by tags and verify results.

        Workflow:
        1. Browse all cases
        2. Filter by specific tag
        3. Verify only cases with that tag are returned

        Validates: Requirements 6.2, 8.1
        """
        # Step 1: Browse all cases
        response = self.client.get("/api/cases/")
        assert response.status_code == 200
        initial_count = len(response.data.get("results", []))
        assert initial_count == 2, "Should have 2 published cases"

        # Step 2: Filter by tag "land-encroachment"
        response = self.client.get("/api/cases/?tags=land-encroachment")
        assert response.status_code == 200

        results = response.data.get("results", [])
        assert len(results) == 1, "Should return 1 case with 'land-encroachment' tag"

        # Step 3: Verify the correct case is returned
        case = results[0]
        assert case["title"] == "Corruption Case - Land Encroachment"
        assert "land-encroachment" in case["tags"]

        # Test filtering by another tag
        response = self.client.get("/api/cases/?tags=infrastructure")
        assert response.status_code == 200

        results = response.data.get("results", [])
        assert len(results) == 1, "Should return 1 case with 'infrastructure' tag"
        assert results[0]["title"] == "Broken Promise - Infrastructure Project"

    def test_search_across_multiple_fields(self):
        """
        E2E Test: Search functionality across title, description, and allegations.

        Workflow:
        1. Search for term in title
        2. Search for term in description
        3. Search for term in key allegations
        4. Verify all searches return correct results

        Validates: Requirements 6.2, 8.1
        """
        # Test 1: Search for term in title
        response = self.client.get("/api/cases/?search=Corruption")
        assert response.status_code == 200

        results = response.data.get("results", [])
        assert len(results) >= 1, "Should find cases with 'Corruption' in title"

        titles = [case["title"] for case in results]
        assert any("Corruption" in title for title in titles)

        # Test 2: Search for term in description
        response = self.client.get("/api/cases/?search=hospital")
        assert response.status_code == 200

        results = response.data.get("results", [])
        assert len(results) >= 1, "Should find cases with 'hospital' in description"

        # Verify the infrastructure case is found
        found_infrastructure_case = any(
            case["title"] == "Broken Promise - Infrastructure Project"
            for case in results
        )
        assert found_infrastructure_case, "Should find the infrastructure case"

        # Test 3: Search for term in key allegations
        response = self.client.get("/api/cases/?search=assets")
        assert response.status_code == 200

        results = response.data.get("results", [])
        assert len(results) >= 1, "Should find cases with 'assets' in allegations"

        # Verify the corruption case is found
        found_corruption_case = any(
            case["title"] == "Corruption Case - Land Encroachment" for case in results
        )
        assert found_corruption_case, "Should find the corruption case"

    def test_single_row_per_case_in_list(self):
        """
        E2E Test: Verify each case_id appears exactly once in the list.

        Workflow:
        1. Create a published case
        2. Edit it in-place
        3. List all cases
        4. Verify the case appears exactly once with updated content

        Validates: Requirements 6.1, 8.3
        """
        # Step 1: Create a published case
        case = create_case_with_entities(
            title="Single Row Case - Original Title",
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
            key_allegations=["Original allegation"],
            case_type=CaseType.CORRUPTION,
            description="Original description",
            state=CaseState.PUBLISHED,
        )

        case_slug = case.slug

        # Step 2: Edit the case in-place
        case.title = "Single Row Case - Updated Title"
        case.description = "Updated description"
        case.save()

        # Step 3: List all cases
        response = self.client.get("/api/cases/")
        assert response.status_code == 200

        results = response.data.get("results", [])

        # Step 4: Verify the case appears exactly once with updated content
        matching_cases = [c for c in results if c["slug"] == case_slug]
        assert len(matching_cases) == 1, "Should only return one row per case_id"

        returned_case = matching_cases[0]
        assert (
            returned_case["title"] == "Single Row Case - Updated Title"
        ), "Should return the current (updated) title"
        assert returned_case["description"] == "Updated description"

    def test_pagination_workflow(self):
        """
        E2E Test: Verify pagination works correctly.

        Workflow:
        1. Create multiple published cases
        2. Request first page
        3. Verify pagination metadata
        4. Request next page if available

        Validates: Requirements 6.1, 8.1
        """
        # Create additional cases to test pagination
        for i in range(5):
            create_case_with_entities(
                title=f"Pagination Test Case {i}",
                alleged_entities=["https://jawafdehi.org/entity/person/test"],
                key_allegations=["Test allegation"],
                case_type=CaseType.CORRUPTION,
                description=f"Test case {i}",
                state=CaseState.PUBLISHED,
            )

        # Request first page
        response = self.client.get("/api/cases/")
        assert response.status_code == 200

        # Verify pagination metadata exists
        assert "count" in response.data, "Response should include total count"
        assert "results" in response.data, "Response should include results"

        # Verify we have results
        results = response.data.get("results", [])
        assert len(results) > 0, "Should have at least some results"

        # Total count should be at least 7 (2 original + 5 new)
        total_count = response.data.get("count", 0)
        assert total_count >= 7, f"Should have at least 7 cases, got {total_count}"
