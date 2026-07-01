"""
Tests for Feedback admin interface.
"""

import pytest
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from cases.admin import FeedbackAdmin
from cases.models import Feedback, FeedbackStatus, FeedbackType
from tests.conftest import create_user_with_role


@pytest.fixture
def admin_user():
    """Create an admin user."""
    return create_user_with_role("admin", "admin@example.com", "Admin")


@pytest.fixture
def feedback_admin():
    """Create a FeedbackAdmin instance."""
    return FeedbackAdmin(Feedback, AdminSite())


@pytest.mark.django_db
class TestFeedbackAdmin:
    """Test suite for Feedback admin interface."""

    def test_admin_can_view_feedback(self, admin_user, feedback_admin):
        """Test that admin can view feedback list."""
        # Create some feedback
        Feedback.objects.create(
            feedback_type=FeedbackType.BUG,
            subject="Test bug",
            description="Test description",
        )

        # Verify feedback appears in queryset
        queryset = feedback_admin.get_queryset(RequestFactory().get("/"))
        assert queryset.count() == 1

    def test_admin_can_change_feedback_status(self, admin_user):
        """Test that admin can change feedback status."""
        feedback = Feedback.objects.create(
            feedback_type=FeedbackType.BUG,
            subject="Test bug",
            description="Test description",
            status=FeedbackStatus.SUBMITTED,
        )

        # Change status
        feedback.status = FeedbackStatus.IN_REVIEW
        feedback.save()

        feedback.refresh_from_db()
        assert feedback.status == FeedbackStatus.IN_REVIEW

    def test_admin_can_add_notes(self, admin_user):
        """Test that admin can add notes to feedback."""
        feedback = Feedback.objects.create(
            feedback_type=FeedbackType.BUG,
            subject="Test bug",
            description="Test description",
        )

        # Add admin notes
        feedback.admin_notes = "Duplicate of issue #123"
        feedback.save()

        feedback.refresh_from_db()
        assert feedback.admin_notes == "Duplicate of issue #123"

    def test_feedback_list_display(self, feedback_admin):
        """Test that feedback list displays correct fields."""
        list_display = feedback_admin.list_display

        assert "id" in list_display
        assert "feedback_type" in list_display
        assert "subject" in list_display
        assert "status" in list_display
        assert "submitted_at" in list_display

    def test_feedback_list_filters(self, feedback_admin):
        """Test that feedback list has correct filters."""
        list_filter = feedback_admin.list_filter

        assert "feedback_type" in list_filter
        assert "status" in list_filter
        assert "submitted_at" in list_filter
