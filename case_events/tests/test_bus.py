# SPDX-License-Identifier: Hippocratic-3.0
"""Tests for the bus: envelope shape, stream topology, and the no-op guarantee.

No broker is involved. The `nats` client is mocked wherever it would be reached,
which is also the point of the design under test — the disabled path must not
even try.
"""

import json
from datetime import datetime, timezone
from unittest import mock

import pytest
from django.test import override_settings

from case_events import bus, streams, subjects
from case_events.envelope import build_envelope


class TestEnvelope:
    def test_carries_the_subject_in_the_body(self):
        # A DLQ'd or archived message must still say what it was.
        env = build_envelope(subject="jaw.case.matched", payload={}, producer="platform")
        assert env["subject"] == "jaw.case.matched"

    def test_timestamps_are_z_suffixed_utc(self):
        env = build_envelope(subject="s", payload={}, producer="p")
        for field in ("occurred_at", "published_at"):
            assert env[field].endswith("Z"), field
            assert "+00:00" not in env[field]

    def test_naive_occurred_at_is_treated_as_utc(self):
        env = build_envelope(
            subject="s", payload={}, producer="p",
            occurred_at=datetime(2026, 7, 30, 12, 0, 0),
        )
        assert env["occurred_at"] == "2026-07-30T12:00:00Z"

    def test_aware_occurred_at_is_converted_to_utc(self):
        tz = timezone(offset=__import__("datetime").timedelta(hours=5, minutes=45))
        env = build_envelope(
            subject="s", payload={}, producer="p",
            occurred_at=datetime(2026, 7, 30, 17, 45, 0, tzinfo=tz),
        )
        assert env["occurred_at"] == "2026-07-30T12:00:00Z"

    def test_occurred_at_defaults_to_now_but_is_distinct_from_published_at(self):
        # Both default to "now", but they are separate fields because a producer
        # that knows the real fact time must be able to set one without the other.
        env = build_envelope(subject="s", payload={}, producer="p")
        assert "occurred_at" in env and "published_at" in env

    def test_subject_refs_default_to_empty_list_not_none(self):
        assert build_envelope(subject="s", payload={}, producer="p")["subject_refs"] == []

    def test_is_json_serialisable_with_nepali(self):
        env = build_envelope(
            subject="s", producer="p",
            payload={"title": "अख्तियार दुरुपयोग अनुसन्धान आयोग"},
        )
        # ensure_ascii=False keeps Devanagari readable on the wire.
        raw = json.dumps(env, ensure_ascii=False)
        assert "अख्तियार" in raw


class TestStreams:
    def test_three_streams_cover_the_three_subject_trees(self):
        by_name = {s.name: s for s in streams.STREAMS}
        assert set(by_name) == {"SIGNALS", "CASE_EVENTS", "DLQ"}
        assert by_name["SIGNALS"].subjects == (subjects.ALL_SIGNALS,)
        assert by_name["CASE_EVENTS"].subjects == (subjects.ALL_CASE_EVENTS,)
        assert by_name["DLQ"].subjects == (subjects.ALL_DLQ,)

    def test_pilot_is_single_replica(self):
        # R1 is a deliberate pilot trade (node-local disk). If this ever changes
        # to 3, the manifests need three pinned nodes and three PVCs first.
        assert all(s.replicas == 1 for s in streams.STREAMS)

    def test_every_case_subject_falls_under_the_case_events_stream(self):
        for subject in (
            subjects.CASE_MATCHED,
            subjects.CASE_UPDATE_PROPOSED,
            subjects.CASE_UPDATE_APPROVED,
            subjects.CASE_UPDATE_REJECTED,
        ):
            assert subject.startswith(subjects.ALL_CASE_EVENTS.rstrip(">"))

    def test_dlq_subject_preserves_the_original(self):
        # A poison message must stay attributable to where it died.
        assert subjects.dlq_subject("jaw.case.matched") == "jaw.dlq.jaw.case.matched"


class TestDisabledByDefault:
    @override_settings(NATS_URL="")
    def test_not_enabled_without_a_url(self):
        assert bus.enabled() is False

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_enabled_with_a_url(self):
        assert bus.enabled() is True

    @override_settings(NATS_URL="")
    def test_publish_is_a_noop_and_never_touches_the_bus(self):
        with mock.patch.object(bus._bus, "publish") as inner:
            assert bus.publish("jaw.case.update.approved", {"x": 1}) is False
        inner.assert_not_called()

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_publish_never_raises_when_the_broker_is_unreachable(self):
        # The core guarantee: a broken bus degrades to False, not an exception.
        with mock.patch.object(bus._bus, "_ensure_started", return_value=False):
            assert bus.publish("jaw.case.update.approved", {"x": 1}) is False

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_publish_swallows_an_unexpected_error(self):
        with mock.patch.object(bus._bus, "publish", side_effect=RuntimeError("boom")):
            assert bus.publish("s", {}) is False


class TestPublishMechanics:
    @override_settings(NATS_URL="nats://localhost:4222")
    def test_dedup_key_becomes_the_nats_msg_id_header(self):
        # This header is what makes JetStream collapse a duplicate publish.
        captured = {}

        def fake_run(coro, loop):
            coro.close()
            return mock.Mock()

        with mock.patch.object(bus._bus, "_ensure_started", return_value=True), \
             mock.patch.object(bus._bus, "_js") as js, \
             mock.patch.object(bus._bus, "_loop", mock.Mock()), \
             mock.patch("case_events.bus.asyncio.run_coroutine_threadsafe", side_effect=fake_run):
            js.publish.side_effect = lambda *a, **kw: captured.update(kw) or mock.Mock()
            bus._bus.publish("s", {"dedup_key": "docket:x:hearing:1"})

        assert captured["headers"] == {"Nats-Msg-Id": "docket:x:hearing:1"}

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_no_header_when_there_is_no_dedup_key(self):
        captured = {}

        def fake_run(coro, loop):
            coro.close()
            return mock.Mock()

        with mock.patch.object(bus._bus, "_ensure_started", return_value=True), \
             mock.patch.object(bus._bus, "_js") as js, \
             mock.patch.object(bus._bus, "_loop", mock.Mock()), \
             mock.patch("case_events.bus.asyncio.run_coroutine_threadsafe", side_effect=fake_run):
            js.publish.side_effect = lambda *a, **kw: captured.update(kw) or mock.Mock()
            bus._bus.publish("s", {"dedup_key": ""})

        assert captured["headers"] is None


class TestRedaction:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("nats://user:secret@host:4222", "nats://***@host:4222"),
            ("nats://host:4222", "nats://host:4222"),
            ("", ""),
        ],
    )
    def test_credentials_never_reach_the_logs(self, url, expected):
        assert bus._redact(url) == expected
