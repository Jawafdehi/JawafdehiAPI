# SPDX-License-Identifier: Hippocratic-3.0
"""The manual-note endpoint.

The property worth guarding is honesty about what happened. This endpoint's
whole job is to hand a fact to an asynchronous pipeline, so the failure that
matters is telling a caseworker "filed" when the note went nowhere — they would
have no reason to look again, and the fact would be lost with no trace anywhere.
"""

from __future__ import annotations

from unittest import mock

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework.test import APIClient

from case_events import subjects
from cases.models import Case, CaseType

pytestmark = pytest.mark.django_db

URL = "/api/signals/manual-note/"


@pytest.fixture(autouse=True)
def _broker(settings):
    """A broker is configured for every test here unless one says otherwise.

    Set through the ``settings`` fixture rather than a class-level
    ``@override_settings``, which silently does nothing on a plain pytest class
    and raises outright on some Django versions.
    """
    settings.NATS_URL = "nats://localhost:4222"


def make_case(slug="lalita-niwas-land-scam"):
    return Case.objects.create(title="Lalita Niwas", case_type=CaseType.CORRUPTION, slug=slug)


_seq = iter(range(1, 10_000))


def client_as(group_name):
    """An authenticated client in ``group_name``.

    Usernames are sequenced because several tests call this twice; a fixed one
    collides on the unique constraint.
    """
    User = get_user_model()
    user = User.objects.create_user(username=f"u-{group_name or 'none'}-{next(_seq)}", password="x")
    if group_name:
        group, _ = Group.objects.get_or_create(name=group_name)
        user.groups.add(group)
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def body(**over):
    return {"case_slug": "lalita-niwas-land-scam", "note": "SC admitted the appeal.", **over}


class TestFiling:
    def test_a_note_reaches_the_bus_as_a_manual_signal(self):
        make_case()
        with mock.patch("case_events.bus.publish", return_value=True) as pub:
            r = client_as("Caseworker").post(URL, body(source="https://ekantipur.com/x"), format="json")

        assert r.status_code == 202, r.data
        subject, envelope = pub.call_args.args[0], pub.call_args.args[1]
        assert subject == subjects.SIGNAL_MANUAL_NOTE
        assert envelope["payload"]["note"] == "SC admitted the appeal."
        assert envelope["source"] == "https://ekantipur.com/x"

    def test_the_case_is_named_outright_so_the_matcher_does_not_have_to_guess(self):
        """A caseworker has told us which case it is; that is an assertion."""
        make_case()
        with mock.patch("case_events.bus.publish", return_value=True) as pub:
            client_as("Caseworker").post(URL, body(), format="json")

        refs = pub.call_args.args[1]["subject_refs"]
        assert refs == ["https://jawafdehi.org/case/lalita-niwas-land-scam"]

    def test_the_response_says_nothing_has_been_written_to_the_case(self):
        make_case()
        with mock.patch("case_events.bus.publish", return_value=True):
            r = client_as("Caseworker").post(URL, body(), format="json")
        assert "until you approve" in r.data["detail"]

    def test_the_same_note_twice_carries_the_same_dedup_key(self):
        make_case()
        with mock.patch("case_events.bus.publish", return_value=True) as pub:
            client_as("Caseworker").post(URL, body(), format="json")
            client_as("Caseworker").post(URL, body(), format="json")

        keys = [call.args[1]["dedup_key"] for call in pub.call_args_list]
        assert keys[0] == keys[1]

    def test_a_different_note_on_the_same_case_is_a_different_fact(self):
        make_case()
        with mock.patch("case_events.bus.publish", return_value=True) as pub:
            client_as("Caseworker").post(URL, body(), format="json")
            client_as("Caseworker").post(URL, body(note="Something else entirely."), format="json")

        keys = [call.args[1]["dedup_key"] for call in pub.call_args_list]
        assert keys[0] != keys[1]

    def test_the_filer_is_recorded(self):
        make_case()
        with mock.patch("case_events.bus.publish", return_value=True) as pub:
            client_as("Caseworker").post(URL, body(), format="json")
        assert pub.call_args.args[1]["payload"]["filed_by"].startswith("caseworker:")

    def test_a_known_fact_date_is_carried_rather_than_the_typing_time(self):
        make_case()
        with mock.patch("case_events.bus.publish", return_value=True) as pub:
            client_as("Caseworker").post(
                URL, body(occurred_at="2026-03-04T00:00:00Z"), format="json"
            )
        assert pub.call_args.args[1]["occurred_at"].startswith("2026-03-04")

    def test_nothing_is_written_to_the_case(self):
        case = make_case()
        before = case.updated_at
        with mock.patch("case_events.bus.publish", return_value=True):
            client_as("Caseworker").post(URL, body(), format="json")
        case.refresh_from_db()
        assert case.updated_at == before


class TestItNeverClaimsSuccessItDidNotHave:
    def test_a_refused_publish_is_a_502_not_a_202(self):
        """Telling a caseworker "filed" when it went nowhere loses the fact."""
        make_case()
        with mock.patch("case_events.bus.publish", return_value=False):
            r = client_as("Caseworker").post(URL, body(), format="json")
        assert r.status_code == 502

    def test_a_raising_bus_is_also_not_a_202(self):
        make_case()
        with mock.patch("case_events.bus.publish", side_effect=RuntimeError("broker gone")):
            r = client_as("Caseworker").post(URL, body(), format="json")
        assert r.status_code == 502

    def test_it_waits_for_the_broker_before_returning_202(self):
        """Without this the two checks above cannot fire on the case that matters.

        Fire-and-forget returns True the moment the publish is scheduled, so a
        broker that rejects the message — which is what a fresh one does for
        every subject until `nats_bootstrap` has run — still produces a 202.
        This endpoint is the documented acceptance test for the whole bus; a
        green light it cannot back up is worse than no endpoint.
        """
        make_case()
        with mock.patch("case_events.bus.publish", return_value=True) as pub:
            r = client_as("Caseworker").post(URL, body(), format="json")
        assert r.status_code == 202
        assert pub.call_args.kwargs.get("wait") is True


class TestNoBrokerConfigured:
    def test_it_refuses_rather_than_silently_dropping_the_note(self, settings):
        settings.NATS_URL = ""
        make_case()
        r = client_as("Caseworker").post(URL, body(), format="json")
        assert r.status_code == 503
        assert "would go nowhere" in r.data["detail"]


class TestValidation:
    def test_an_unknown_case_is_rejected_up_front(self):
        """Otherwise it publishes, matches nothing, and is dropped in silence."""
        r = client_as("Caseworker").post(URL, body(case_slug="no-such-case"), format="json")
        assert r.status_code == 400
        assert "case_slug" in r.data

    def test_a_slug_the_iri_grammar_rejects_is_refused(self):
        make_case()
        r = client_as("Caseworker").post(URL, body(case_slug="Not_A_Slug"), format="json")
        assert r.status_code == 400

    @pytest.mark.parametrize("note", ["", "   "])
    def test_a_blank_note_is_rejected(self, note):
        make_case()
        r = client_as("Caseworker").post(URL, body(note=note), format="json")
        assert r.status_code == 400

    def test_the_note_is_trimmed(self):
        make_case()
        with mock.patch("case_events.bus.publish", return_value=True) as pub:
            client_as("Caseworker").post(URL, body(note="  padded  "), format="json")
        assert pub.call_args.args[1]["payload"]["note"] == "padded"


class TestPermissions:
    def test_an_anonymous_caller_cannot_file(self):
        make_case()
        assert APIClient().post(URL, body(), format="json").status_code in (401, 403)

    def test_a_reader_cannot_file(self):
        make_case()
        assert client_as("ReadOnly").post(URL, body(), format="json").status_code == 403

    def test_the_automation_role_cannot_generate_its_own_work(self):
        """A note becomes a model call and then a human's queue item.

        JobPoller may create proposals — automation is the primary producer —
        but it must not be able to commission them.
        """
        make_case()
        assert client_as("JobPoller").post(URL, body(), format="json").status_code == 403

    def test_a_caseworker_can(self):
        make_case()
        with mock.patch("case_events.bus.publish", return_value=True):
            assert client_as("Caseworker").post(URL, body(), format="json").status_code == 202
