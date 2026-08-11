"""
Tests for Feedback admin interface.
"""

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.files.uploadedfile import SimpleUploadedFile
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

    def test_admin_is_view_only(self, admin_user, feedback_admin):
        """Triage moved to the SPA panel, so Django admin must not offer edits.

        Asserted against a SUPERUSER, who holds every model permission — if the
        gate holds for them it holds for everyone, and a pass here cannot be an
        accident of some missing permission.
        """
        feedback = Feedback.objects.create(
            feedback_type=FeedbackType.BUG,
            subject="Test bug",
            description="Test description",
            status=FeedbackStatus.SUBMITTED,
        )
        request = RequestFactory().get("/")
        request.user = admin_user

        assert feedback_admin.has_add_permission(request) is False
        assert feedback_admin.has_change_permission(request) is False
        assert feedback_admin.has_change_permission(request, feedback) is False

    def test_admin_keeps_delete_and_view(self, admin_user, feedback_admin):
        """Deleting stays here (destructive, and this is the only surface that
        shows the reporter's contact details), as does reading."""
        feedback = Feedback.objects.create(
            feedback_type=FeedbackType.BUG,
            subject="Test bug",
            description="Test description",
        )
        request = RequestFactory().get("/")
        request.user = admin_user

        assert feedback_admin.has_delete_permission(request) is True
        assert feedback_admin.has_delete_permission(request, feedback) is True
        assert feedback_admin.has_view_permission(request) is True
        assert feedback_admin.has_view_permission(request, feedback) is True

    def test_the_change_page_actually_renders_read_only(self, admin_user, client):
        """GET the real page, not just the permission predicates.

        Turning off change permission makes Django take a path this admin never
        took before: ``get_form`` replaces ``readonly_fields`` with the flattened
        fieldsets, so ``attachment`` (a FileField, rendered via
        ``display_for_field`` → ``value.url``) and the ``contact_info`` JSONField
        go through ``AdminReadonlyField`` instead of a widget. Asserting only on
        ``has_change_permission()`` would leave that rendering untested, and this
        page is the escalation path the SPA's "ask an administrator" hint points
        at — it must not 500.
        """
        feedback = Feedback.objects.create(
            feedback_type=FeedbackType.CASE_REPORT,
            subject="Alleged irregularity",
            description="Details of the allegation.",
            contact_info={
                "name": "Test Reporter",
                "contactMethods": [{"type": "email", "value": "reporter@example.com"}],
            },
            attachment=SimpleUploadedFile("evidence.txt", b"attachment body"),
        )
        client.force_login(admin_user)

        response = client.get(
            f"/django-admin/cases/feedback/{feedback.pk}/change/"
        )

        assert response.status_code == 200
        body = response.content.decode()
        # Readable...
        assert "Alleged irregularity" in body
        # ...and this is the ONE surface that still shows the reporter, which is
        # why the SPA can afford to omit them.
        assert "reporter@example.com" in body
        # ...but with no way to submit a change.
        assert 'name="_save"' not in body

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
