# SPDX-License-Identifier: Hippocratic-3.0
"""Tests for the bus: envelope shape, stream topology, and the no-op guarantee.

No broker is involved. The `nats` client is mocked wherever it would be reached,
which is also the point of the design under test — the disabled path must not
even try.
"""

import asyncio
import inspect
import json
import threading
import time
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

    def test_occurred_at_and_published_at_are_independent(self):
        """The gap between them is the producer-lag figure.

        Asserting only that both keys exist could not see ``published_at`` being
        derived from ``occurred_at`` — which collapses the two for any producer
        that knows its real fact time, destroying the number entirely.
        """
        past = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        env = build_envelope(subject="s", payload={}, producer="p", occurred_at=past)
        assert env["occurred_at"] == "2020-01-02T03:04:05Z"
        assert env["published_at"] != env["occurred_at"]
        assert env["published_at"] > "2024", "published_at must be now, not the fact time"

    def test_provenance_fields_are_carried_through(self):
        env = build_envelope(
            subject="s",
            payload={},
            producer="consumer:matcher",
            source="https://x.test/doc",
            raw_ref="r2://bucket/key",
            dedup_key="k-1",
        )
        assert env["producer"] == "consumer:matcher"
        assert env["source"] == "https://x.test/doc"
        assert env["raw_ref"] == "r2://bucket/key"
        assert env["dedup_key"] == "k-1"

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
        # Asserted as LITERALS, not against the constants they are built from.
        # Comparing streams.STREAMS to subjects.ALL_* only proves the module
        # references the constant: changing ALL_CASE_EVENTS to "jaw.>" used to
        # pass every test here while making the streams overlap, which JetStream
        # rejects — so every process would have died at startup.
        by_name = {s.name: s.subjects for s in streams.STREAMS}
        assert by_name == {
            "SIGNALS": ("jaw.signal.>",),
            "CASE_EVENTS": ("jaw.case.>",),
            "DLQ": ("jaw.dlq.>",),
        }

    def test_no_two_streams_claim_overlapping_subjects(self):
        # JetStream refuses to create a stream whose subjects overlap another's.
        prefixes = [s.subjects[0].rstrip(">") for s in streams.STREAMS]
        for i, a in enumerate(prefixes):
            for b in prefixes[i + 1 :]:
                assert not (a.startswith(b) or b.startswith(a)), f"{a} overlaps {b}"

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
            assert subject.startswith("jaw.case.")

    def test_every_signal_subject_falls_under_the_signals_stream(self):
        for name in dir(subjects):
            if name.startswith("SIGNAL_"):
                assert getattr(subjects, name).startswith("jaw.signal.")


class TestEnsureStreams:
    """What is actually handed to JetStream, not just what the dataclass holds.

    ``test_pilot_is_single_replica`` reads like coverage of the R1 decision but
    asserts the dataclass field; the value passed to ``add_stream`` is a separate
    expression, and hardcoding ``num_replicas=3`` there used to survive the whole
    suite.
    """

    @staticmethod
    def _fresh_broker():
        """A JetStream mock where no stream exists yet.

        The ``stream_info`` side effect is load-bearing and easy to omit: a bare
        ``AsyncMock`` answers every call successfully, so ``stream_info`` "finds"
        every stream and ``ensure_streams`` takes the UPDATE path. A test that
        forgets this asserts nothing about creation while looking like it does —
        which is how the first draft of these tests passed with ``add_stream``
        never called at all.
        """
        js = mock.AsyncMock()
        js.stream_info.side_effect = RuntimeError("stream not found")
        return js

    async def _created_configs(self):
        js = self._fresh_broker()
        await streams.ensure_streams(js)
        return [call.args[0] for call in js.add_stream.await_args_list]

    async def test_asserts_every_stream_with_the_configured_values(self):
        configs = await self._created_configs()
        assert [c.name for c in configs] == ["SIGNALS", "CASE_EVENTS", "DLQ"]
        for config in configs:
            # File storage: a memory stream silently loses everything on a
            # broker restart, which for a year-long retention window is not a
            # degraded bus but a broken one.
            assert config.storage == "file", config.name
            assert config.num_replicas == 1, config.name

    async def test_signals_is_trimmed_harder_than_the_records_are(self):
        """The two streams that are RECORDS keep a year; the noisy one does not.

        SIGNALS receives every observation EIGHT times, because the docket producer
        is stateless and rescans a 48h window every 6h — ~19k messages and ~12MB a
        day, which a year-long window would turn into ~4.5GB against an 8GiB
        JetStream store shared with CASE_EVENTS.
        """
        by_name = {c.name: c for c in await self._created_configs()}
        assert by_name["SIGNALS"].max_age == streams.SEVEN_DAYS_SECONDS
        assert by_name["CASE_EVENTS"].max_age == streams.ONE_YEAR_SECONDS
        assert by_name["DLQ"].max_age == streams.ONE_YEAR_SECONDS

    async def test_only_signals_carries_a_byte_ceiling(self):
        """A backstop, not the trimming mechanism.

        Publishes into a full JetStream store FAIL, and ``handle_matcher`` raises on
        a failed publish — so an unbounded noise stream is a route for noise to
        wedge the audit trail.
        """
        by_name = {c.name: c for c in await self._created_configs()}
        assert by_name["SIGNALS"].max_bytes == streams.ONE_GIBIBYTE
        assert by_name["CASE_EVENTS"].max_bytes == -1
        assert by_name["DLQ"].max_bytes == -1

    async def test_an_existing_stream_is_updated_rather_than_re_created(self):
        """The bug this fixes. ``add_stream`` alone is idempotent only in the
        trivial sense: JetStream rejects a CREATE whose config differs from the
        live stream, so editing STREAMS used to fail against a real broker with
        "stream name already in use" — making the spec table a record of what the
        streams were when first created, not what they should be."""
        js = mock.AsyncMock()  # every stream_info succeeds -> all three exist
        await streams.ensure_streams(js)

        assert js.add_stream.await_count == 0
        assert [c.args[0].name for c in js.update_stream.await_args_list] == [
            "SIGNALS",
            "CASE_EVENTS",
            "DLQ",
        ]

    async def test_losing_a_create_race_converges_instead_of_failing(self):
        """Two bootstraps can interleave between the check and the create. The
        loser's job is to leave the stream matching the spec, which an update
        does."""
        js = self._fresh_broker()
        js.add_stream.side_effect = RuntimeError("stream name already in use")

        await streams.ensure_streams(js)

        assert js.update_stream.await_count == len(streams.STREAMS)

    async def test_a_client_error_is_not_swallowed(self):
        # Unlike publishing, this is deliberately NOT best-effort: a consumer
        # that cannot see its stream should fail loudly at startup. Asserted on
        # the UPDATE path, since that is the one a running broker takes.
        js = mock.AsyncMock()
        js.update_stream.side_effect = RuntimeError("jetstream unavailable")
        with pytest.raises(RuntimeError):
            await streams.ensure_streams(js)

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
    @staticmethod
    def _publish(envelope, subject="jaw.case.update.approved", wait=False):
        """Drive _Bus.publish with the loop and JetStream context stubbed out.

        Returns (positional_args, kwargs, return_value) from the js.publish call.
        The positional args matter as much as the headers — capturing only
        kwargs left the subject and body unasserted, so publishing everything to
        a hardcoded wrong subject used to pass.
        """
        seen = {}

        def fake_run(coro, loop):
            coro.close()
            return mock.Mock()

        with mock.patch.object(bus._bus, "_ensure_started", return_value=True), \
             mock.patch.object(bus._bus, "_js") as js, \
             mock.patch.object(bus._bus, "_loop", mock.Mock()), \
             mock.patch("case_events.bus.asyncio.run_coroutine_threadsafe", side_effect=fake_run):
            def record(*args, **kwargs):
                seen["args"], seen["kwargs"] = args, kwargs
                return mock.Mock()

            js.publish.side_effect = record
            result = bus._bus.publish(subject, envelope, wait=wait)

        return seen.get("args"), seen.get("kwargs"), result

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_the_subject_and_body_reach_jetstream(self):
        env = {"dedup_key": "k", "payload": {"case_slug": "lalita-niwas"}}
        args, _kwargs, result = self._publish(env, subject="jaw.case.update.approved")
        assert args[0] == "jaw.case.update.approved"
        assert json.loads(args[1].decode("utf-8")) == env
        assert result is True

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_devanagari_survives_the_wire_encoding(self):
        env = {"payload": {"title": "अख्तियार दुरुपयोग अनुसन्धान आयोग"}}
        args, _kwargs, _ = self._publish(env)
        # ensure_ascii=False, so it is real UTF-8 rather than \u escapes.
        assert "अख्तियार".encode("utf-8") in args[1]

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_dedup_key_becomes_the_nats_msg_id_header(self):
        # This header is what makes JetStream collapse a duplicate publish.
        _args, kwargs, _ = self._publish({"dedup_key": "docket:x:hearing:1"})
        assert kwargs["headers"] == {"Nats-Msg-Id": "docket:x:hearing:1"}

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_no_header_when_there_is_no_dedup_key(self):
        _args, kwargs, _ = self._publish({"dedup_key": ""})
        assert kwargs["headers"] is None

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_a_fire_and_forget_failure_is_logged(self):
        """Nothing else observes a wait=False publish.

        ``_log_result`` is the only thing standing between a rejected publish and
        total silence, and it had no test at all — dropping the done-callback
        entirely used to pass the suite.
        """
        failed = mock.Mock()
        failed.result.side_effect = RuntimeError("nak")
        with mock.patch.object(bus.logger, "warning") as warn:
            bus._log_result(failed, "jaw.case.update.approved")
        assert warn.call_args.kwargs["subject"] == "jaw.case.update.approved"

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_a_successful_fire_and_forget_logs_nothing(self):
        ok = mock.Mock()
        ok.result.return_value = None
        with mock.patch.object(bus.logger, "warning") as warn:
            bus._log_result(ok, "s")
        warn.assert_not_called()

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_a_loop_that_died_under_us_does_not_orphan_the_coroutine(self):
        """A failed schedule must close the coroutine it already built.

        ``js.publish(...)`` is eager, but ``run_coroutine_threadsafe`` raises on a
        closed loop *before* wrapping it in a task, so nothing will ever await it and
        it emits "coroutine was never awaited" when collected. That would be a SECOND
        suite warning, which AGENTS.md treats as a real signal.

        Asserted on the coroutine's own state rather than by catching the warning,
        because the warning only fires at GC time and would make this flaky.
        """
        built = {}

        async def real_publish(*_args, **_kwargs):  # a genuine coroutine, not a Mock
            return None

        def capture(*args, **kwargs):
            built["coro"] = real_publish(*args, **kwargs)
            return built["coro"]

        with mock.patch.object(bus._bus, "_ensure_started", return_value=True), \
             mock.patch.object(bus._bus, "_js") as js, \
             mock.patch.object(bus._bus, "_loop", mock.Mock()), \
             mock.patch(
                 "case_events.bus.asyncio.run_coroutine_threadsafe",
                 side_effect=RuntimeError("Event loop is closed"),
             ), \
             mock.patch.object(bus.logger, "warning") as warn:
            js.publish.side_effect = capture
            result = bus._bus.publish("jaw.case.update.approved", {"payload": {}})

        assert result is False
        assert warn.call_args.kwargs["error"] == "Event loop is closed"
        assert inspect.getcoroutinestate(built["coro"]) == inspect.CORO_CLOSED


class TestOnlyOneThreadPaysTheConnectCost:
    """A dead broker must not stall every publishing thread.

    The connect can take the full STARTUP_TIMEOUT_SECONDS and on a dead broker
    always does, because ``max_reconnect_attempts=-1`` stops nats-py ever
    failing the initial connect fast. It used to run while holding the lock, so
    one outage froze all 8 gthread workers for 5s at a time.
    """

    def test_a_second_thread_does_not_queue_behind_a_slow_connect(self):
        b = bus._Bus()
        connecting = threading.Event()
        release = threading.Event()

        def slow_start():
            connecting.set()
            release.wait(10)
            raise RuntimeError("no broker")

        with mock.patch.object(b, "_start", side_effect=slow_start):
            first = threading.Thread(target=b._ensure_started, daemon=True)
            first.start()
            assert connecting.wait(5), "the first thread never began connecting"

            began = time.monotonic()
            assert b._ensure_started() is False
            elapsed = time.monotonic() - began

            release.set()
            first.join(10)

        assert elapsed < 0.5, f"second thread blocked for {elapsed:.2f}s behind the connect"

    def test_a_successful_connect_clears_the_retry_window(self):
        b = bus._Bus()
        # Outside the window, so the start is attempted.
        b._last_failure_at = time.monotonic() - bus.CONNECT_RETRY_SECONDS - 1
        state = (mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
        with mock.patch.object(b, "_start", return_value=state):
            assert b._ensure_started() is True
        assert b._last_failure_at == 0.0, "a working broker must not stay backed off"

    def test_a_failed_connect_arms_the_retry_window_and_clears_starting(self):
        b = bus._Bus()
        with mock.patch.object(b, "_start", side_effect=RuntimeError("boom")):
            assert b._ensure_started() is False
        assert b._last_failure_at > 0
        assert b._starting is False, "a failed start must not wedge the bus"

    def test_a_timeout_is_logged_with_its_type_not_an_empty_string(self):
        # str(TimeoutError()) is "", which made the one line explaining an
        # outage read `connect_failed error=`.
        b = bus._Bus()
        with mock.patch.object(b, "_start", side_effect=TimeoutError()):
            with mock.patch.object(bus.logger, "warning") as warn:
                assert b._ensure_started() is False
        kwargs = warn.call_args.kwargs
        assert kwargs["error_type"] == "TimeoutError"
        assert kwargs["error"], "the error field must never be empty"

    def test_reset_clears_starting_so_a_forked_child_can_connect(self):
        b = bus._Bus()
        b._starting = True
        with b._lock:
            b._reset_locked()
        assert b._starting is False


class TestTheLoopIsClosedNotJustStopped:
    """``loop.stop()`` leaves the selector and its self-pipe socketpair open.

    Harmless for orderly shutdown, which happens once — but ``_start`` takes the
    same path on every failed connect, and with a 30-second retry window a
    sustained broker outage leaks a couple of descriptors per attempt in a
    process that never restarts.
    """

    def test_a_failed_connect_closes_the_loop_it_abandoned(self):
        b = bus._Bus()
        loops = []
        real_new_event_loop = asyncio.new_event_loop

        def track():
            loop = real_new_event_loop()
            loops.append(loop)
            return loop

        with mock.patch.object(bus.asyncio, "new_event_loop", track):
            with mock.patch.object(b, "_connect", side_effect=RuntimeError("no broker")):
                assert b._ensure_started() is False

        assert loops, "the failure path must still have created a loop to abandon"
        assert all(loop.is_closed() for loop in loops)

    def test_close_closes_the_loop_too(self):
        b = bus._Bus()
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        b._loop, b._thread, b._nc, b._js = loop, thread, None, mock.Mock()

        b.close()

        assert loop.is_closed()

    def test_shutdown_finishes_the_tasks_still_running_on_the_loop(self):
        """A cancelled connect is still mid-flight when the loop is stopped.

        `future.cancel()` only SCHEDULES cancellation. Stop the loop straight
        after and the task is still pending — holding, in the real case, a
        half-open TCP socket — so closing discards it ("Task was destroyed but
        it is pending!") along with the descriptor the close exists to reclaim.
        """
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        async def never_finishes():
            await asyncio.sleep(30)

        asyncio.run_coroutine_threadsafe(never_finishes(), loop)

        async def grab_others():
            return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

        pending = asyncio.run_coroutine_threadsafe(grab_others(), loop).result(timeout=2)
        assert pending and not pending[0].done(), "fixture did not produce a live task"

        bus._shutdown_loop(loop, thread)

        assert loop.is_closed()
        assert pending[0].done(), "a task was still pending when the loop was closed"

    def test_a_thread_that_will_not_stop_leaks_rather_than_hangs(self):
        """Shutdown must not block a request on a wedged loop thread."""
        stuck = mock.Mock()
        stuck.is_alive.return_value = True
        loop = mock.Mock()

        bus._shutdown_loop(loop, stuck)

        loop.close.assert_not_called()  # closing a running loop raises
        stuck.join.assert_called_once()


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
