"""
Tests for role-based authorization to access DRAFT cases via GET /cases/<id>.

Originally these exercised the DRF auth-token transport; after the OIDC-only
migration (which removed DRF token auth) they use force_authenticate to set
request.user directly. The behavior under test — which roles can read DRAFT
cases — is unchanged and auth-scheme-agnostic.
"""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from cases.models import CaseState, CaseType
from tests.conftest import create_case_with_entities

User = get_user_model()


@pytest.mark.django_db
class TestTokenAuthDraftCases:
    """Test role-based access to DRAFT cases."""

    def setup_method(self):
        """Set up test data for each test."""
        self.client = APIClient()

        # Create users with different roles
        self.admin_user = User.objects.create_user(
            username="admin", password="password"
        )
        self.admin_user.is_superuser = True
        self.admin_user.save()

        self.contributor_user = User.objects.create_user(
            username="contributor", password="password"
        )
        contributor_group, _ = Group.objects.get_or_create(name="Caseworker")
        self.contributor_user.groups.add(contributor_group)

        self.other_contributor = User.objects.create_user(
            username="other_contributor", password="password"
        )
        self.other_contributor.groups.add(contributor_group)

    def test_draft_case_not_accessible_without_authorization(self):
        """DRAFT case should return 404 for unauthenticated/unauthorized requests."""
        # Create a DRAFT case
        case = create_case_with_entities(
            title="Draft Case",
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
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
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.DRAFT,
        )

        # Access as admin
        self.client.force_authenticate(user=self.admin_user)
        response = self.client.get(f"/api/cases/{case.slug}/")

        assert response.status_code == 200
        assert response.data["slug"] == case.slug
        assert response.data["state"] == CaseState.DRAFT

    def test_draft_case_accessible_to_authorized_contributor(self):
        """DRAFT case should be accessible to authorized contributor (assigned to case)."""
        # Create a DRAFT case
        case = create_case_with_entities(
            title="Draft Case",
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.DRAFT,
        )

        # Assign contributor to the case
        case.contributors.add(self.contributor_user)

        # Access as contributor
        self.client.force_authenticate(user=self.contributor_user)
        response = self.client.get(f"/api/cases/{case.slug}/")

        assert response.status_code == 200
        assert response.data["slug"] == case.slug
        assert response.data["state"] == CaseState.DRAFT

    def test_draft_case_accessible_to_any_contributor(self):
        """DRAFT case should be accessible to any contributor (global read access)."""
        # Create a DRAFT case assigned to contributor_user
        case = create_case_with_entities(
            title="Draft Case",
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.DRAFT,
        )
        case.contributors.add(self.contributor_user)

        # Access as other_contributor (not assigned to case)
        self.client.force_authenticate(user=self.other_contributor)
        response = self.client.get(f"/api/cases/{case.slug}/")

        assert response.status_code == 200
        assert response.data["slug"] == case.slug

    def test_published_case_accessible_without_authorization(self):
        """PUBLISHED case should be accessible without authorization."""
        # Create a PUBLISHED case
        case = create_case_with_entities(
            title="Published Case",
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.PUBLISHED,
        )

        # Access without authentication
        response = self.client.get(f"/api/cases/{case.slug}/")

        assert response.status_code == 200
        assert response.data["slug"] == case.slug
        assert response.data["state"] == CaseState.PUBLISHED

    def test_in_review_case_requires_authorization(self):
        """IN_REVIEW is casework: anonymous gets 404, a casework-role user gets 200.

        Under the new role model (public = readonly EXCEPT no casework), an
        IN_REVIEW case is casework and is no longer publicly retrievable.
        """
        # Create an IN_REVIEW case
        case = create_case_with_entities(
            title="In Review Case",
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.IN_REVIEW,
        )

        # Anonymous (public) access is denied with 404.
        response = self.client.get(f"/api/cases/{case.slug}/")
        assert response.status_code == 404

        # A casework-role user (Caseworker) can retrieve the in-review case.
        self.client.force_authenticate(user=self.contributor_user)
        response = self.client.get(f"/api/cases/{case.slug}/")

        assert response.status_code == 200
        assert response.data["slug"] == case.slug
        assert response.data["state"] == CaseState.IN_REVIEW

    def test_list_endpoint_shows_published_and_assigned_for_contributor(self):
        """Contributor list view should include PUBLISHED and their assigned draft cases."""
        # Create cases in different states
        draft_case = create_case_with_entities(
            title="Draft Case",
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.DRAFT,
        )
        draft_case.contributors.add(self.contributor_user)

        published_case = create_case_with_entities(
            title="Published Case",
            alleged_entities=["https://jawafdehi.org/entity/person/test"],
            key_allegations=["Test allegation"],
            case_type=CaseType.CORRUPTION,
            description="Test description",
            state=CaseState.PUBLISHED,
        )

        # Access list endpoint as contributor
        self.client.force_authenticate(user=self.contributor_user)
        response = self.client.get("/api/cases/")

        assert response.status_code == 200
        case_ids = [c["slug"] for c in response.data["results"]]

        # Contributor should see published + assigned draft case
        assert published_case.slug in case_ids
        assert draft_case.slug in case_ids
