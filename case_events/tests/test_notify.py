# SPDX-License-Identifier: LicenseRef-Hippocratic-3.0
"""The outbound webhook: what it says, and what it is still not allowed to carry.

This module used to refuse to forward the drafted change at all, and two tests
here asserted exactly that. They are gone, replaced by the narrower rule that
took over — see the :mod:`case_events.notify` docstring for why the old one was
sound in principle and failed in practice. What remains negative is the part that
is still true: a case is only linked when that link works, a caseworker's handle
never leaves, and neither does the webhook URL.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
from django.test import override_settings

from case_events import notify

# `post` now resolves the case slug to decide whether it may link the case, so
# every test here touches the ORM even when it asserts nothing about a case. The
# slug below deliberately resolves to nothing, which is its own interesting
# branch and the one most proposals in the queue would hit for a retired slug.
pytestmark = [pytest.mark.django_db(databases="__all__")]

PAYLOAD = {
    "proposal_id": 7,
    "case_slug": "lalita-niwas-land-scam",
    "status": "pending",
    "confidence": 0.88,
    "reviewer": None,
    # The drafted change. Forwarded now — see the module docstring.
    "intent": {
        "type": "append_timeline_entry",
        "entry": {"title": "विशेष अदालतबाट सफाईको फैसला", "description": "…judges named…"},
    },
    "case_title": "Lalita Niwas land scam",
}

WEBHOOK = "https://example.test/webhooks/abc/secret-token"


def posted(mock_urlopen):
    """The decoded JSON body of the single POST made."""
    request = mock_urlopen.call_args.args[0]
    return json.loads(request.data.decode("utf-8"))


class _Response:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestWhatLeavesTheBuilding:
    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_the_drafted_change_is_forwarded_now(self):
        """The reversal, asserted rather than left implicit.

        A reviewer has to be able to tell an acquittal being recorded from a
        hearing being adjourned without opening the queue, which is what the
        pointer-only message could not do.
        """
        with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            assert notify.post(PAYLOAD) is True

        content = posted(urlopen)["content"]
        assert "विशेष अदालतबाट सफाईको फैसला" in content
        assert "Lalita Niwas land scam" in content

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_the_draft_is_marked_as_a_draft(self):
        """Quoted, so a reader can tell the model's sentence from the case facts.

        Without this the drafted title sits in the same visual register as the
        case status and the court it came from, and reads as something already
        established.
        """
        with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            notify.post(PAYLOAD)

        content = posted(urlopen)["content"]
        assert "**Proposal details**" in content
        assert "> विशेष अदालतबाट सफाईको फैसला" in content

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_the_caseworker_handle_never_leaves(self):
        """``detected_by`` holds a personal email address on hand-filed proposals."""
        with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            notify.post({**PAYLOAD, "detected_by": "caseworker:someone@gmail.test"})

        assert "someone@gmail.test" not in json.dumps(posted(urlopen), ensure_ascii=False)

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_confidence_is_kept_for_machines_and_hidden_from_readers(self):
        """It is the same number on 201 of 345 proposals: it reads as information
        while carrying none, so it stays out of the prose and in the fields."""
        with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            notify.post(PAYLOAD)

        body = posted(urlopen)
        assert body["confidence"] == 0.88
        assert "0.88" not in body["content"]

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_it_carries_enough_to_act_on(self):
        with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            notify.post(PAYLOAD)

        body = posted(urlopen)
        assert body["proposal_id"] == 7
        assert body["case_slug"] == "lalita-niwas-land-scam"
        assert body["status"] == "pending"
        # Chat receivers require a rendered line; a structured-only body renders
        # as an empty message in Discord and as nothing useful anywhere else.
        assert "content" in body and body["content"]
        # Deep-links the proposal, not the queue it is one of 344 rows in.
        assert body["url"].endswith(f"{notify.QUEUE_PATH}/7")

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_the_link_preview_is_suppressed_both_ways(self):
        """Every URL unfurled the site's generic og: card — identical boilerplate,
        taller than the message, once per proposal. The flag binds a Discord
        receiver; the angle brackets bind the markdown."""
        with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            notify.post(PAYLOAD)

        body = posted(urlopen)
        assert body["flags"] == notify.SUPPRESS_EMBEDS
        assert "](<https://" in body["content"]


class TestItCannotPingAnyone:
    """Raised in review, and a gap the module's own docstring did not cover: that
    docstring bounds what the payload CONTAINS; this is about what it can DO."""

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_a_mention_smuggled_through_a_case_slug_is_defanged(self):
        """`case_slug` derives from a title a caseworker wrote, so it is
        human-authored text reaching a rendered chat line."""
        with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            notify.post({**PAYLOAD, "case_slug": "@everyone-scandal"})

        content = posted(urlopen)["content"]
        assert "@everyone" not in content
        assert f"@{notify.ZERO_WIDTH_SPACE}everyone" in content

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_a_mention_in_the_reviewer_handle_is_defanged_too(self):
        """The other human-authored field: an account handle.

        Pinned to a DECIDED proposal on purpose. The reviewer is only rendered on
        a decision, so running this against the default pending payload would
        assert the absence of a string that was never going to be there — green
        for the wrong reason, and green even if the defanging were deleted.
        """
        with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            notify.post({**PAYLOAD, "status": "approved", "reviewer": "@here"})

        content = posted(urlopen)["content"]
        assert "@here" not in content
        assert f"@{notify.ZERO_WIDTH_SPACE}here" in content

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_mentions_are_refused_at_the_receiver_as_well(self):
        """The escaping is belt; this is braces. With `parse` empty a Discord
        receiver resolves no mention even from text that escaped defanging."""
        with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            notify.post(PAYLOAD)

        assert posted(urlopen)["allowed_mentions"] == {"parse": []}

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_the_structured_fields_are_left_verbatim(self):
        """Only the RENDERED prose is escaped. Mangling an identifier a receiver
        might match on would be worse than the risk being guarded."""
        with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            notify.post({**PAYLOAD, "case_slug": "@everyone-scandal"})

        assert posted(urlopen)["case_slug"] == "@everyone-scandal"

    def test_the_zero_width_space_is_not_a_literal_in_the_source(self):
        """A literal U+200B is invisible in source: an editor, a reformat or a
        careless selection can delete it with nothing looking wrong afterwards.
        Written as an escape so the guard stays legible."""
        import pathlib

        source = pathlib.Path(notify.__file__).read_text()
        assert notify.ZERO_WIDTH_SPACE not in source, "use the \\u200b escape, not the character"
        assert notify.ZERO_WIDTH_SPACE == "\u200b"


class TestItNeverBreaksTheConsumer:
    def test_no_url_configured_is_a_silent_no_op(self):
        """Log-only is the documented default, so this must not warn or raise —
        and must not attempt a request."""
        with override_settings(CASE_EVENTS_WEBHOOK_URL=""):
            with mock.patch("urllib.request.urlopen") as urlopen:
                assert notify.post(PAYLOAD) is False
            urlopen.assert_not_called()

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    @pytest.mark.parametrize(
        "boom",
        [
            TimeoutError("timed out"),
            OSError("dns"),
            ValueError("unknown url type"),
        ],
    )
    def test_a_transport_failure_is_swallowed(self, boom):
        """Raising would redeliver the JetStream message, and redelivery would
        re-notify rather than re-do anything useful."""
        with mock.patch("urllib.request.urlopen", side_effect=boom):
            assert notify.post(PAYLOAD) is False

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_a_rejection_is_reported_without_logging_the_url(self, caplog):
        """A revoked webhook 404s forever and silently. It has to be visible — but
        the URL is the credential, so it must not reach the log that makes it
        visible."""
        import urllib.error

        err = urllib.error.HTTPError(WEBHOOK, 404, "Not Found", {}, None)
        with caplog.at_level("WARNING"):
            with mock.patch("urllib.request.urlopen", side_effect=err):
                assert notify.post(PAYLOAD) is False

        assert "404" in caplog.text
        assert "secret-token" not in caplog.text

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_a_non_2xx_answer_is_not_counted_as_delivered(self):
        with mock.patch("urllib.request.urlopen", return_value=_Response(status=302)):
            assert notify.post(PAYLOAD) is False

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_204_counts_as_delivered(self):
        """Discord answers 204 with no body; pinning 200 would read every
        successful post as a failure."""
        with mock.patch("urllib.request.urlopen", return_value=_Response(status=204)):
            assert notify.post(PAYLOAD) is True


class TestTheHandlerUsesIt:
    @pytest.mark.django_db
    def test_the_log_line_survives_a_webhook_that_is_down(self, caplog):
        """The log is the record; the webhook is a convenience. If an outage could
        suppress the record, the notifier would be less useful than before it had
        a channel at all."""
        from case_events.consumers import handlers

        with override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK):
            with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
                with caplog.at_level("INFO"):
                    handlers.handle_notifier({"subject": "jaw.case.update.proposed", "payload": PAYLOAD}, None)

        assert "caseworker_notified" in caplog.text

    @pytest.mark.django_db
    def test_the_handler_posts_when_configured(self):
        from case_events.consumers import handlers

        with override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK):
            with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
                handlers.handle_notifier({"subject": "jaw.case.update.proposed", "payload": PAYLOAD}, None)

        assert urlopen.called


def make_case(slug="lalita-niwas-land-scam", state="PUBLISHED", **kwargs):
    from cases.models import Case

    return Case.objects.create(
        slug=slug,
        title="अख्तियार दुरुपयोग अनुसन्धान आयोग वि. लाल सिंह विष्ट",
        state=state,
        **kwargs,
    )


class TestEachIntentTypeRenders:
    """Every shape in the queue, because three of the four break the obvious code.

    The counts are from the 345 proposals filed so far: 320 timeline entries, 14
    material links, 11 hand-filed patches. A detail block written for the first
    renders nothing for the other two.
    """

    def test_a_timeline_entry_leads_with_the_bs_date(self):
        """BS first: it is the date the court record carries, and the AD date is a
        conversion of it."""
        body = notify.build_message(
            {
                "proposal_id": 341,
                "case_slug": "a-case",
                "intent": {
                    "type": "append_timeline_entry",
                    "entry": {
                        "date": "2014-12-18",
                        "date_bs": "2071-09-03",
                        "title": "विशेष अदालतले फैसला सुनायो",
                    },
                },
            }
        )

        assert "> **2071-09-03** · 2014-12-18" in body["content"]
        assert "Add timeline entry" in body["content"]

    def test_a_material_link_shows_the_iri_unmasked(self):
        """The IRI is the identifier a reviewer may want to read and copy, so it is
        shown rather than hidden behind a label — inside <> so it still does not
        unfurl."""
        iri = "https://jawafdehi.org/material/court_order/special.080-cr-0066"
        body = notify.build_message(
            {
                "proposal_id": 109,
                "case_slug": "a-case",
                "intent": {"type": "link_material", "material": iri, "relation": "court order"},
            }
        )

        assert f"<{iri}>" in body["content"]
        assert 'proposed as "court order"' in body["content"]

    def test_a_raw_patch_is_summarised_not_dumped(self):
        body = notify.build_message(
            {
                "proposal_id": 77,
                "case_slug": "a-case",
                "intent": {
                    "type": "raw_patch",
                    "patch": [{"op": "replace", "path": "/timeline", "value": [{"x": 1}]}],
                },
            }
        )

        assert "1 change: replace /timeline" in body["content"]
        # The value is the whole new timeline. Dumping it would bury the message.
        assert '"x": 1' not in body["content"]

    def test_a_patch_that_cannot_be_applied_says_so(self):
        """Ten of the eleven hand-filed patches in the queue target `entities`,
        which raw_patch is not permitted to touch, so approving them 400s. Finding
        that out by clicking approve is the expensive way."""
        body = notify.build_message(
            {
                "proposal_id": 58,
                "case_slug": "a-case",
                "intent": {
                    "type": "raw_patch",
                    "patch": [
                        {"op": "replace", "path": "/entities/18/notes", "value": "x"},
                        {"op": "replace", "path": "/entities/18/outcome", "value": "y"},
                    ],
                },
            }
        )

        assert "⚠️" in body["content"]
        assert "entities" in body["content"]

    def test_an_unknown_intent_type_says_so_rather_than_rendering_nothing(self):
        """A new intent type must produce a message that admits it, not one that
        silently omits the only thing the reviewer needs."""
        body = notify.build_message(
            {"proposal_id": 1, "case_slug": "a-case", "intent": {"type": "teleport_case"}}
        )

        assert "teleport_case" in body["content"]


class TestDegradedCases:
    """The shapes a template written against the happy path renders wrongly."""

    def test_a_blank_case_title_falls_back_to_the_slug(self):
        """Every hand-filed proposal in the queue has one, so `Case: ""` is not
        hypothetical."""
        body = notify.build_message(
            {"proposal_id": 77, "case_slug": "mdms", "case_title": "", "intent": {"type": "raw_patch"}}
        )

        assert "mdms" in body["content"]

    def test_an_unresolvable_slug_is_named_not_linked(self):
        """7 of the 293 slugs in the queue resolve to no case at all; linking one
        sends the reviewer to a 404."""
        body = notify.build_message(
            {"proposal_id": 51, "case_slug": "case-080-cr-0047-gone", "case_title": "A case"},
            case=None,
        )

        assert "no case found" in body["content"]
        assert "/case/case-080-cr-0047-gone" not in body["content"]

    def test_an_unpublished_case_is_not_linked(self):
        """DRAFT and IN_REVIEW cases 404 for anyone without a casework role, so the
        link would be dead for most readers and would assert that unpublished
        editorial work is public."""
        case = make_case(slug="still-in-review", state="IN_REVIEW")
        body = notify.build_message(
            {"proposal_id": 1, "case_slug": case.slug, "case_title": "Not public yet"}, case=case
        )

        assert "not published" in body["content"]
        assert "/case/still-in-review" not in body["content"]

    def test_a_published_case_is_linked_and_carries_both_statuses(self):
        case = make_case()
        body = notify.build_message(
            {"proposal_id": 1, "case_slug": case.slug, "case_title": "A published case"}, case=case
        )

        assert f"](<https://jawafdehi.org/case/{case.slug}>)" in body["content"]
        assert "**Case status:**" in body["content"]

    def test_a_decision_does_not_look_like_an_arrival(self):
        """They used to differ by one bolded word mid-sentence, which is invisible
        in a scrolling channel."""
        arrived = notify.build_message({"proposal_id": 1, "status": "pending"})["content"]
        approved = notify.build_message(
            {"proposal_id": 1, "status": "approved", "reviewer": "caseworker:someone"}
        )["content"]

        assert arrived.startswith("**A new case proposal has arrived!**")
        assert not approved.startswith("**A new case proposal has arrived!**")
        assert "approved" in approved
