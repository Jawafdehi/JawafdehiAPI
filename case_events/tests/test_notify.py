# SPDX-License-Identifier: Hippocratic-3.0
"""The outbound webhook, and what it is not allowed to carry.

Most of these are about restraint rather than function: a PENDING proposal is
unreviewed model output naming real people in corruption cases, and this is the
one code path that sends anything about it to a third party. The assertions that
matter are the negative ones.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest
from django.test import override_settings

from case_events import notify

PAYLOAD = {
    "proposal_id": 7,
    "case_slug": "lalita-niwas-land-scam",
    "status": "pending",
    "confidence": 0.88,
    "reviewer": None,
    # Present in the real envelope, and must NOT be forwarded.
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
    def test_the_drafted_intent_is_never_forwarded(self):
        """The whole reason this module exists separately.

        Sending the draft would publish an unreviewed model claim about named
        individuals to a third party with its own retention, before any human
        agreed with it. The review queue's entire purpose is that this does not
        happen.
        """
        with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            assert notify.post(PAYLOAD) is True

        body = json.dumps(posted(urlopen), ensure_ascii=False)
        assert "intent" not in posted(urlopen)
        assert "सफाईको" not in body
        assert "judges named" not in body

    @override_settings(CASE_EVENTS_WEBHOOK_URL=WEBHOOK)
    def test_the_case_title_is_not_forwarded_either(self):
        """A slug is an identifier; a title is editorial prose about an accusation.
        The reviewer resolves the slug in the app, behind SSO."""
        with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            notify.post(PAYLOAD)

        assert "Lalita Niwas land scam" not in json.dumps(posted(urlopen))

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
        assert body["url"].endswith(notify.QUEUE_PATH)


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
        """The other human-authored field: an account handle."""
        with mock.patch("urllib.request.urlopen", return_value=_Response()) as urlopen:
            notify.post({**PAYLOAD, "reviewer": "@here"})

        assert "@here" not in posted(urlopen)["content"]

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
