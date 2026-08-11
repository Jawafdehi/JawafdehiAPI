"""Tests for the staff feedback read/triage API (``/api/feedback-submissions/``).

Two properties matter more than the CRUD mechanics and are asserted hardest:

1. The endpoint never discloses who reported something. ``contact_info``,
   ``ip_address`` and ``user_agent`` are absent from every response shape, and
   no filter, search or ordering parameter brings them back.
2. Triage cannot rewrite the submission. Only ``status`` and ``adminNotes``
   move; everything the reporter wrote is read-only.
"""

import pytest
from rest_framework.test import APIClient

from cases.models import Feedback, FeedbackStatus, FeedbackType
from tests.conftest import create_user_with_role

LIST_URL = "/api/feedback-submissions/"
DETAIL_URL = "/api/feedback-submissions/{}/"

# Every key that would identify or locate a reporter, in both the model's
# snake_case and the API's camelCase spelling — a serializer added under either
# name must fail this.
PII_KEYS = (
    "contact_info",
    "contactInfo",
    "ip_address",
    "ipAddress",
    "user_agent",
    "userAgent",
    "attachment",
)


def _authed_client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _feedback(**kwargs) -> Feedback:
    defaults = dict(
        feedback_type=FeedbackType.BUG,
        subject="Search is broken",
        description="Searching for a case returns nothing.",
        related_page="Cases page",
        ip_address="203.0.113.9",
        user_agent="Mozilla/5.0 (test)",
        contact_info={
            "name": "राम बहादुर",
            "contactMethods": [{"type": "email", "value": "ram@example.com"}],
        },
    )
    defaults.update(kwargs)
    return Feedback.objects.create(**defaults)


@pytest.fixture
def caseworker():
    return create_user_with_role("cw", "cw@example.com", "Caseworker")


@pytest.fixture
def superuser():
    return create_user_with_role("admin", "admin@example.com", "Admin")


@pytest.fixture
def readonly():
    return create_user_with_role("ro", "ro@example.com", "ReadOnly")


@pytest.mark.django_db
class TestFeedbackTriageAccess:
    """Admin + caseworker only. ReadOnly is deliberately excluded."""

    def test_anonymous_cannot_list(self):
        _feedback()
        response = APIClient().get(LIST_URL)
        assert response.status_code in (401, 403)

    def test_readonly_role_cannot_list(self, readonly):
        """ReadOnly reads the platform, not the public's messages to it.

        This is the one place the systemwide-read invariant is knowingly not
        applied, so it gets an explicit test rather than being left to drift.
        """
        _feedback()
        response = _authed_client(readonly).get(LIST_URL)
        assert response.status_code == 403

    def test_caseworker_can_list(self, caseworker):
        _feedback()
        response = _authed_client(caseworker).get(LIST_URL)
        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_superuser_can_list(self, superuser):
        _feedback()
        response = _authed_client(superuser).get(LIST_URL)
        assert response.status_code == 200
        assert response.json()["count"] == 1

    def test_anonymous_cannot_triage(self):
        feedback = _feedback()
        response = APIClient().patch(
            DETAIL_URL.format(feedback.pk), {"status": "resolved"}, format="json"
        )
        assert response.status_code in (401, 403)
        feedback.refresh_from_db()
        assert feedback.status == FeedbackStatus.SUBMITTED

    def test_readonly_cannot_triage(self, readonly):
        feedback = _feedback()
        response = _authed_client(readonly).patch(
            DETAIL_URL.format(feedback.pk), {"status": "resolved"}, format="json"
        )
        assert response.status_code == 403
        feedback.refresh_from_db()
        assert feedback.status == FeedbackStatus.SUBMITTED


@pytest.mark.django_db
class TestFeedbackTriagePrivacy:
    """The reporter's identity never crosses this endpoint."""

    def test_list_omits_reporter_identity(self, caseworker):
        _feedback()
        row = _authed_client(caseworker).get(LIST_URL).json()["results"][0]

        for key in PII_KEYS:
            assert key not in row, f"{key} leaked into the staff feedback list"
        # Not just absent as keys — the values themselves are nowhere in the body.
        body = str(row)
        assert "203.0.113.9" not in body
        assert "ram@example.com" not in body
        assert "राम बहादुर" not in body

    def test_detail_omits_reporter_identity(self, caseworker):
        feedback = _feedback()
        row = _authed_client(caseworker).get(DETAIL_URL.format(feedback.pk)).json()

        for key in PII_KEYS:
            assert key not in row
        assert "ram@example.com" not in str(row)

    def test_triage_response_omits_reporter_identity(self, caseworker):
        """The PATCH response is a third response shape — check it too."""
        feedback = _feedback()
        row = (
            _authed_client(caseworker)
            .patch(
                DETAIL_URL.format(feedback.pk), {"status": "in_review"}, format="json"
            )
            .json()
        )

        for key in PII_KEYS:
            assert key not in row
        assert "ram@example.com" not in str(row)

    def test_presence_is_exposed_without_the_values(self, caseworker):
        """"There is contact info you can't see here" is what tells a triager
        to escalate to someone who can."""
        with_contact = _feedback()
        without = _feedback(subject="No contact", contact_info={})

        rows = {
            r["id"]: r
            for r in _authed_client(caseworker).get(LIST_URL).json()["results"]
        }
        assert rows[with_contact.pk]["hasContactInfo"] is True
        assert rows[without.pk]["hasContactInfo"] is False
        assert rows[with_contact.pk]["hasAttachment"] is False

    def test_a_name_alone_counts_as_contact_info(self, caseworker):
        """``name`` without ``contactMethods`` is a valid submission.

        Reporting ``false`` for it would tell a triager the report is anonymous
        while the reporter's name sits in the row — the exact mistake that leads
        to forwarding a "safe" report onward, or to answering an erasure request
        with "we hold no personal data".
        """
        named = _feedback(subject="Name only", contact_info={"name": "A Reporter"})

        row = _authed_client(caseworker).get(DETAIL_URL.format(named.pk)).json()

        assert row["hasContactInfo"] is True
        assert "A Reporter" not in str(row)

    def test_reported_content_is_readable(self, caseworker):
        """Triage would be pointless without the report itself."""
        feedback = _feedback()
        row = _authed_client(caseworker).get(DETAIL_URL.format(feedback.pk)).json()

        assert row["subject"] == "Search is broken"
        assert row["description"] == "Searching for a case returns nothing."
        assert row["relatedPage"] == "Cases page"
        assert row["feedbackType"] == "bug"


@pytest.mark.django_db
class TestFeedbackTriageWrites:
    def test_caseworker_sets_status(self, caseworker):
        feedback = _feedback()
        response = _authed_client(caseworker).patch(
            DETAIL_URL.format(feedback.pk), {"status": "in_review"}, format="json"
        )

        assert response.status_code == 200
        feedback.refresh_from_db()
        assert feedback.status == FeedbackStatus.IN_REVIEW

    def test_caseworker_sets_admin_notes(self, caseworker):
        feedback = _feedback()
        response = _authed_client(caseworker).patch(
            DETAIL_URL.format(feedback.pk),
            {"adminNotes": "Duplicate of #123"},
            format="json",
        )

        assert response.status_code == 200
        feedback.refresh_from_db()
        assert feedback.admin_notes == "Duplicate of #123"

    def test_invalid_status_is_rejected(self, caseworker):
        feedback = _feedback()
        response = _authed_client(caseworker).patch(
            DETAIL_URL.format(feedback.pk), {"status": "wontfix"}, format="json"
        )

        assert response.status_code == 400
        feedback.refresh_from_db()
        assert feedback.status == FeedbackStatus.SUBMITTED

    def test_reporter_fields_are_immutable(self, caseworker):
        """A triager records a decision; they don't get to edit the report."""
        feedback = _feedback()
        response = _authed_client(caseworker).patch(
            DETAIL_URL.format(feedback.pk),
            {
                "subject": "Rewritten by staff",
                "description": "Rewritten body",
                "relatedPage": "Elsewhere",
                "status": "in_review",
            },
            format="json",
        )

        assert response.status_code == 200
        feedback.refresh_from_db()
        assert feedback.subject == "Search is broken"
        assert feedback.description == "Searching for a case returns nothing."
        assert feedback.related_page == "Cases page"
        # The one field that was supposed to move, moved.
        assert feedback.status == FeedbackStatus.IN_REVIEW

    def test_a_submission_can_be_reclassified(self, caseworker):
        """Re-filing is a triage decision, and this is now the only surface for it.

        The public form lets anyone pick "general" for what is actually a
        corruption allegation. Only ``case_report`` gets the notification and the
        distinct treatment in the queue, and Django admin can no longer edit, so
        without this the misfiling would be permanent.
        """
        feedback = _feedback(feedback_type=FeedbackType.GENERAL)

        response = _authed_client(caseworker).patch(
            DETAIL_URL.format(feedback.pk),
            {"feedbackType": "case_report"},
            format="json",
        )

        assert response.status_code == 200
        feedback.refresh_from_db()
        assert feedback.feedback_type == FeedbackType.CASE_REPORT

    def test_reclassifying_to_an_unknown_type_is_rejected(self, caseworker):
        feedback = _feedback()
        response = _authed_client(caseworker).patch(
            DETAIL_URL.format(feedback.pk),
            {"feedbackType": "not_a_type"},
            format="json",
        )

        assert response.status_code == 400
        feedback.refresh_from_db()
        assert feedback.feedback_type == FeedbackType.BUG

    def test_triage_never_writes_a_pii_column(self, caseworker, django_assert_num_queries):
        """The write is column-scoped, so a PATCH cannot touch reporter data.

        ``Feedback.save()`` runs ``full_clean()``, which reads every concrete
        field — that would un-defer ip_address/user_agent and emit a
        full-column UPDATE rewriting them. The serializer writes via a scoped
        QuerySet.update() instead; this asserts the columns survive untouched
        and that no extra SELECT pulled them into the process.
        """
        feedback = _feedback()

        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            response = _authed_client(caseworker).patch(
                DETAIL_URL.format(feedback.pk), {"status": "resolved"}, format="json"
            )
        assert response.status_code == 200

        # Match on the statement VERB, not a substring — every SELECT of this
        # table contains the column `updated_at`, which contains "update".
        updates = [
            q["sql"]
            for q in ctx.captured_queries
            if q["sql"].lstrip().upper().startswith("UPDATE")
        ]
        assert updates, "expected an UPDATE"
        for sql in updates:
            assert "ip_address" not in sql
            assert "user_agent" not in sql
            assert "contact_info" not in sql
            assert "attachment" not in sql

        feedback.refresh_from_db()
        assert feedback.status == FeedbackStatus.RESOLVED
        assert feedback.ip_address == "203.0.113.9"
        assert feedback.user_agent == "Mozilla/5.0 (test)"
        assert feedback.contact_info["contactMethods"][0]["value"] == "ram@example.com"

    def test_triage_still_lands_in_the_audit_trail(self, caseworker):
        """A scoped UPDATE skips post_save, so the entry is written explicitly.

        Without this the whole point of AuditlogActorMixin would be lost: the
        edit would happen with no record of who made it.
        """
        from auditlog.models import LogEntry

        feedback = _feedback()
        _authed_client(caseworker).patch(
            DETAIL_URL.format(feedback.pk),
            {"status": "in_review", "adminNotes": "Looking into it"},
            format="json",
        )

        entries = LogEntry.objects.filter(
            content_type__model="feedback",
            object_pk=str(feedback.pk),
            action=LogEntry.Action.UPDATE,
        )
        # EXACTLY one. AuditedQuerySet.update() already logs the write, so an
        # explicit log_bulk_update() alongside it double-records every triage
        # edit — which is what this asserted before the count was pinned.
        assert entries.count() == 1
        entry = entries.get()
        assert "status" in entry.changes
        assert entry.actor == caseworker

    def test_triage_bumps_updated_at(self, caseworker):
        """auto_now doesn't fire for QuerySet.update(), so it's stamped by hand.

        The queue shows "last updated" from this column; if it stopped moving,
        a triaged submission would look untouched.
        """
        feedback = _feedback()
        before = feedback.updated_at

        _authed_client(caseworker).patch(
            DETAIL_URL.format(feedback.pk), {"status": "in_review"}, format="json"
        )

        feedback.refresh_from_db()
        assert feedback.updated_at > before

    def test_put_is_not_allowed(self, caseworker):
        """Only two fields are writable, so a full replace has no meaning."""
        feedback = _feedback()
        response = _authed_client(caseworker).put(
            DETAIL_URL.format(feedback.pk), {"status": "resolved"}, format="json"
        )
        assert response.status_code == 405

    def test_staff_cannot_create_feedback_here(self, caseworker):
        response = _authed_client(caseworker).post(
            LIST_URL,
            {"feedbackType": "bug", "subject": "x", "description": "y"},
            format="json",
        )
        assert response.status_code == 405

    def test_staff_cannot_delete_feedback_here(self, caseworker):
        """Destructive removal stays with the superuser in Django admin."""
        feedback = _feedback()
        response = _authed_client(caseworker).delete(DETAIL_URL.format(feedback.pk))
        assert response.status_code == 405
        assert Feedback.objects.filter(pk=feedback.pk).exists()


@pytest.mark.django_db
class TestFeedbackTriageQueue:
    def test_filter_by_status(self, caseworker):
        _feedback(subject="Open one")
        _feedback(subject="Done one", status=FeedbackStatus.RESOLVED)

        response = _authed_client(caseworker).get(LIST_URL, {"status": "resolved"})
        results = response.json()["results"]
        assert [r["subject"] for r in results] == ["Done one"]

    def test_filter_by_type(self, caseworker):
        _feedback(subject="A bug")
        _feedback(subject="A report", feedback_type=FeedbackType.CASE_REPORT)

        response = _authed_client(caseworker).get(
            LIST_URL, {"feedback_type": "case_report"}
        )
        results = response.json()["results"]
        assert [r["subject"] for r in results] == ["A report"]

    def test_search_spans_subject_and_description(self, caseworker):
        _feedback(subject="Procurement irregularity", description="Body one")
        _feedback(subject="Other", description="Mentions procurement too")
        _feedback(subject="Unrelated", description="Nothing here")

        response = _authed_client(caseworker).get(LIST_URL, {"search": "procurement"})
        assert response.json()["count"] == 2

    def test_newest_first_by_default(self, caseworker):
        first = _feedback(subject="Older")
        second = _feedback(subject="Newer")
        assert first.submitted_at <= second.submitted_at

        results = _authed_client(caseworker).get(LIST_URL).json()["results"]
        assert [r["subject"] for r in results] == ["Newer", "Older"]

    def test_page_size_is_client_sizable(self, caseworker):
        for i in range(3):
            _feedback(subject=f"Item {i}")

        body = _authed_client(caseworker).get(LIST_URL, {"page_size": 2}).json()
        assert body["count"] == 3
        assert len(body["results"]) == 2
        assert body["next"] is not None


@pytest.mark.django_db
class TestPublicSubmissionStillWorks:
    """The staff route must not have disturbed the public one."""

    def test_anonymous_can_still_submit(self):
        response = APIClient().post(
            "/api/feedback/",
            {
                "feedbackType": "general",
                "subject": "Nice work",
                "description": "The platform is helpful.",
            },
            format="json",
        )
        assert response.status_code == 201

    def test_public_route_still_refuses_reads(self):
        """``/api/feedback/`` stays write-only; reading moved to its own path,
        so this must not have become a listing of everyone's submissions."""
        _feedback()
        response = APIClient().get("/api/feedback/")
        assert response.status_code == 405
