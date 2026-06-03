"""
Tests for the public Feedback API endpoint.
"""

import io

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from cases.models import Feedback, FeedbackStatus, FeedbackType


@pytest.fixture
def api_client():
    """Create an API client for testing."""
    return APIClient()


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear cache before each test."""
    cache.clear()
    yield
    cache.clear()


@pytest.mark.django_db
class TestFeedbackSubmission:
    """Test suite for feedback submission."""

    def test_submit_minimal_feedback(self, api_client):
        """Test submitting feedback with only required fields."""
        data = {
            "feedbackType": "general",
            "subject": "Great platform",
            "description": "This platform is very helpful",
        }

        response = api_client.post("/api/feedback/", data, format="json")
        assert response.status_code == 201

        response_data = response.json()
        assert response_data["feedbackType"] == "general"
        assert response_data["status"] == "submitted"
        assert "id" in response_data

        feedback = Feedback.objects.get(id=response_data["id"])
        assert feedback.feedback_type == FeedbackType.GENERAL
        assert feedback.status == FeedbackStatus.SUBMITTED

    def test_submit_feedback_with_contact_info(self, api_client):
        """Test submitting feedback with contact information."""
        data = {
            "feedbackType": "bug",
            "subject": "Search not working",
            "description": "Detailed bug description",
            "contactInfo": {
                "name": "राम बहादुर",
                "contactMethods": [{"type": "email", "value": "ram@example.com"}],
            },
        }

        response = api_client.post("/api/feedback/", data, format="json")
        assert response.status_code == 201

        feedback = Feedback.objects.get(id=response.json()["id"])
        assert feedback.contact_info["name"] == "राम बहादुर"

    def test_submit_all_feedback_types(self, api_client):
        """Test submitting all feedback types."""
        types = ["bug", "feature", "usability", "content", "general"]

        for feedback_type in types:
            data = {
                "feedbackType": feedback_type,
                "subject": f"Test {feedback_type}",
                "description": "Test description",
            }

            response = api_client.post("/api/feedback/", data, format="json")
            assert response.status_code == 201


@pytest.mark.django_db
class TestFeedbackValidation:
    """Test suite for feedback validation."""

    def test_missing_required_fields(self, api_client):
        """Test that required fields are validated."""
        # Missing feedbackType
        response = api_client.post(
            "/api/feedback/", {"subject": "Test", "description": "Test"}, format="json"
        )
        assert response.status_code == 400

        # Missing subject
        response = api_client.post(
            "/api/feedback/",
            {"feedbackType": "bug", "description": "Test"},
            format="json",
        )
        assert response.status_code == 400

        # Missing description
        response = api_client.post(
            "/api/feedback/", {"feedbackType": "bug", "subject": "Test"}, format="json"
        )
        assert response.status_code == 400

    def test_invalid_feedback_type(self, api_client):
        """Test that invalid feedback type is rejected."""
        data = {
            "feedbackType": "invalid_type",
            "subject": "Test",
            "description": "Test description",
        }

        response = api_client.post("/api/feedback/", data, format="json")
        assert response.status_code == 400


@pytest.mark.django_db
class TestFeedbackRateLimiting:
    """Test suite for feedback rate limiting."""

    def test_rate_limit_allows_five_submissions(self, api_client):
        """Test that 5 submissions within an hour are allowed."""
        data = {
            "feedbackType": "general",
            "subject": "Test",
            "description": "Test description",
        }

        for i in range(5):
            response = api_client.post(
                "/api/feedback/", data, format="json", REMOTE_ADDR="192.168.1.100"
            )
            assert response.status_code == 201

        assert Feedback.objects.count() == 5

    def test_rate_limit_blocks_sixth_submission(self, api_client):
        """Test that 6th submission within an hour is blocked."""
        data = {
            "feedbackType": "general",
            "subject": "Test",
            "description": "Test description",
        }

        for _ in range(5):
            api_client.post(
                "/api/feedback/", data, format="json", REMOTE_ADDR="192.168.1.100"
            )

        response = api_client.post(
            "/api/feedback/", data, format="json", REMOTE_ADDR="192.168.1.100"
        )
        assert response.status_code == 429
        assert Feedback.objects.count() == 5

    def test_rate_limit_per_ip_address(self, api_client):
        """Test that rate limit is per IP address."""
        data = {
            "feedbackType": "general",
            "subject": "Test",
            "description": "Test description",
        }

        # 5 from first IP
        for _ in range(5):
            api_client.post(
                "/api/feedback/", data, format="json", REMOTE_ADDR="192.168.1.100"
            )

        # 6th from same IP blocked
        response = api_client.post(
            "/api/feedback/", data, format="json", REMOTE_ADDR="192.168.1.100"
        )
        assert response.status_code == 429

        # Different IP succeeds
        response = api_client.post(
            "/api/feedback/", data, format="json", REMOTE_ADDR="192.168.1.200"
        )
        assert response.status_code == 201


@pytest.mark.django_db
class TestFeedbackFileUpload:
    """Test suite for feedback file attachment uploads."""

    @pytest.fixture(autouse=True)
    def _tmp_media_root(self, settings, tmp_path):
        """Redirect MEDIA_ROOT to a temp directory to avoid leftover files."""
        settings.MEDIA_ROOT = tmp_path

    def _make_file(
        self, size_bytes: int, name: str = "test.png", content_type: str = "image/png"
    ):
        """Create an in-memory file of the given size."""
        data = io.BytesIO(b"x" * size_bytes)
        data.name = name
        return data

    def test_submit_feedback_with_attachment(self, api_client):
        """Test submitting feedback with a valid file attachment."""
        attachment = self._make_file(1024, name="screenshot.png")

        response = api_client.post(
            "/api/feedback/",
            data={
                "feedbackType": "bug",
                "subject": "Bug with screenshot",
                "description": "Here is a screenshot of the issue.",
                "attachment": attachment,
            },
            format="multipart",
        )

        assert response.status_code == 201
        feedback = Feedback.objects.get(id=response.json()["id"])
        assert feedback.attachment is not None
        assert feedback.attachment.name.startswith("feedback_attachments/")

    def test_submit_feedback_without_attachment(self, api_client):
        """Test that attachment is optional — JSON submission still works."""
        data = {
            "feedbackType": "general",
            "subject": "No file",
            "description": "This is text-only feedback.",
        }

        response = api_client.post("/api/feedback/", data, format="json")
        assert response.status_code == 201

        feedback = Feedback.objects.get(id=response.json()["id"])
        assert not feedback.attachment

    def test_attachment_over_10mb_rejected(self, api_client):
        """Test that attachments over 10 MB are rejected with 400."""
        oversized = self._make_file(10 * 1024 * 1024 + 1, name="big.png")

        response = api_client.post(
            "/api/feedback/",
            data={
                "feedbackType": "bug",
                "subject": "Big file",
                "description": "This file is too large.",
                "attachment": oversized,
            },
            format="multipart",
        )

        assert response.status_code == 400
        assert "attachment" in response.json().get("details", {})

    def test_attachment_exactly_10mb_accepted(self, api_client):
        """Test that a file of exactly 10 MB is accepted."""
        exact = self._make_file(10 * 1024 * 1024, name="exact.png")

        response = api_client.post(
            "/api/feedback/",
            data={
                "feedbackType": "bug",
                "subject": "Exactly 10 MB",
                "description": "Edge case file size.",
                "attachment": exact,
            },
            format="multipart",
        )

        assert response.status_code == 201
