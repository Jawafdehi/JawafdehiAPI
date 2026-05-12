"""
Tests for token-based authorization to access DRAFT cases.

Feature: Allow optional token-based authorization for GET /cases/<id> endpoint
"""

import pytest
from rest_framework.test import APIClient
from rest_framework.authtoken.models import Token
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from cases.models import CaseState, CaseType
from tests.conftest import create_case_with_entities

User = get_user_model()


@pytest.mark.django_db
class TestTokenAuthDraftCases:
    """Test token-based access to DRAFT cases."""

    def setup_method(self):
        """Set up test data for each test."""
        self.client = APIClient()

        # Create users with different roles
        self.admin_user = User.objects.create_user(
            username="admin", password="password"
        )
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.admin_token = Token.objects.create(user=self.admin_user)

        self.contributor_user = User.objects.create_user(
            username="contributor", password="password"
        )
        contributor_group, _ = Group.objects.get_or_create(name="Contributor")
        self.contributor_user.groups.add(contributor_group)
        self.contributor_token = Token.objects.create(user=self.contributor_user)

        self.other_contributor = User.objects.create_user(
            username="other_contributor", password="password"
        )
        self.other_contributor.groups.add(contributor_group)
        self.other_contributor_token = Token.objects.create(user=self.other_contributor)

    def test_draft_case_not_accessible_without_authorization(self):
        """DRAFT case should return 404 for unauthenticated/unauthorized requests."""
        # Create a DRAFT case
        case = create_case_with_entities(
            title="Draft Case",
            alleged_entities=["entity:person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.DRAFT,
        )

        # Try to access without authentication
        response = self.client.get(f"/api/cases/{case.slug}/")

        assert response.status_code == 404

    def test_draft_case_accessible_to_authorized_admin(self):
        """DRAFT case should be accessible to authorized admin."""
        # Create a DRAFT case
        case = create_case_with_entities(
            title="Draft Case",
            alleged_entities=["entity:person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.DRAFT,
        )

        # Access with admin token
        self.client.credentials(HTTP_AUTHORIZATION=f"Token {self.admin_token.key}")
        response = self.client.get(f"/api/cases/{case.slug}/")

        assert response.status_code == 200
        assert response.data["case_id"] == case.case_id
        assert response.data["state"] == CaseState.DRAFT

    def test_draft_case_accessible_to_authorized_contributor(self):
        """DRAFT case should be accessible to authorized contributor (assigned to case)."""
        # Create a DRAFT case
        case = create_case_with_entities(
            title="Draft Case",
            alleged_entities=["entity:person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.DRAFT,
        )

        # Assign contributor to the case
        case.contributors.add(self.contributor_user)

        # Access with contributor token
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {self.contributor_token.key}"
        )
        response = self.client.get(f"/api/cases/{case.slug}/")

        assert response.status_code == 200
        assert response.data["case_id"] == case.case_id
        assert response.data["state"] == CaseState.DRAFT

    def test_draft_case_not_accessible_to_unauthorized_contributor(self):
        """DRAFT case should NOT be accessible to unauthorized contributor (not assigned to case)."""
        # Create a DRAFT case assigned to contributor_user
        case = create_case_with_entities(
            title="Draft Case",
            alleged_entities=["entity:person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.DRAFT,
        )
        case.contributors.add(self.contributor_user)

        # Try to access with other_contributor token (not assigned)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {self.other_contributor_token.key}"
        )
        response = self.client.get(f"/api/cases/{case.slug}/")

        assert response.status_code == 404
        assert response.data["detail"] == "Not found."

    def test_published_case_accessible_without_authorization(self):
        """PUBLISHED case should be accessible without authorization."""
        # Create a PUBLISHED case
        case = create_case_with_entities(
            title="Published Case",
            alleged_entities=["entity:person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.PUBLISHED,
        )

        # Access without authentication
        response = self.client.get(f"/api/cases/{case.slug}/")

        assert response.status_code == 200
        assert response.data["case_id"] == case.case_id
        assert response.data["state"] == CaseState.PUBLISHED

    def test_in_review_case_accessible_without_authorization(self):
        """IN_REVIEW case should be accessible without authorization."""
        # Create an IN_REVIEW case
        case = create_case_with_entities(
            title="In Review Case",
            alleged_entities=["entity:person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.IN_REVIEW,
        )

        # Access without authentication
        response = self.client.get(f"/api/cases/{case.slug}/")

        assert response.status_code == 200
        assert response.data["case_id"] == case.case_id
        assert response.data["state"] == CaseState.IN_REVIEW

    def test_list_endpoint_shows_published_and_assigned_for_contributor(self):
        """Contributor list view should include PUBLISHED and their assigned draft cases."""
        # Create cases in different states
        draft_case = create_case_with_entities(
            title="Draft Case",
            alleged_entities=["entity:person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.DRAFT,
        )
        draft_case.contributors.add(self.contributor_user)

        published_case = create_case_with_entities(
            title="Published Case",
            alleged_entities=["entity:person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.PUBLISHED,
        )

        # Access list endpoint with contributor token
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Token {self.contributor_token.key}"
        )
        response = self.client.get("/api/cases/")

        assert response.status_code == 200
        case_ids = [c["case_id"] for c in response.data["results"]]

        # Contributor should see published + assigned draft case
        assert published_case.case_id in case_ids
        assert draft_case.case_id in case_ids
