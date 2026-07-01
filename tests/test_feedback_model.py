"""
Unit tests for the Feedback model.
"""

import pytest

from cases.models import Feedback, FeedbackStatus, FeedbackType


@pytest.mark.django_db
class TestFeedbackModel:
    """Test suite for Feedback model."""

    def test_create_feedback_with_required_fields(self):
        """Test creating feedback with only required fields."""
        feedback = Feedback.objects.create(
            feedback_type=FeedbackType.BUG,
            subject="Search not working",
            description="Detailed description of the bug",
        )

        assert feedback.id is not None
        assert feedback.feedback_type == FeedbackType.BUG
        assert feedback.status == FeedbackStatus.SUBMITTED
        assert feedback.contact_info == {}

    def test_create_feedback_with_contact_info(self):
        """Test creating feedback with contact information."""
        contact_info = {
            "name": "राम बहादुर",
            "contactMethods": [{"type": "email", "value": "ram@example.com"}],
        }

        feedback = Feedback.objects.create(
            feedback_type=FeedbackType.FEATURE,
            subject="Add dark mode",
            description="Please add dark mode support",
            contact_info=contact_info,
        )

        assert feedback.contact_info == contact_info

    def test_feedback_str_representation(self):
        """Test string representation of feedback."""
        feedback = Feedback.objects.create(
            feedback_type=FeedbackType.BUG,
            subject="Search not working",
            description="Test",
        )

        assert str(feedback) == "BUG: Search not working"

    def test_status_transitions(self):
        """Test feedback status transitions."""
        feedback = Feedback.objects.create(
            feedback_type=FeedbackType.BUG,
            subject="Test",
            description="Test description",
        )

        feedback.status = FeedbackStatus.IN_REVIEW
        feedback.save()
        feedback.refresh_from_db()
        assert feedback.status == FeedbackStatus.IN_REVIEW

        feedback.status = FeedbackStatus.RESOLVED
        feedback.save()
        feedback.refresh_from_db()
        assert feedback.status == FeedbackStatus.RESOLVED
