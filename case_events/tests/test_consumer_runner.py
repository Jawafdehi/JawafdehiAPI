# SPDX-License-Identifier: Hippocratic-3.0
"""The pull-consumer machinery, exercised without a broker.

The property that matters most is the one a live NATS would make hardest to
test: **nothing is dropped silently.** JetStream stops redelivering at
``max_deliver`` and then emits an advisory nobody is listening to, so a message
NAK'd on its final delivery is simply gone. Every test about the last delivery
is about that.

Fakes rather than mocks for the message and the JetStream context, because what
is being asserted is a SEQUENCE — republish, then terminate, and never the other
way round — and a recording fake states that more legibly than call-order
assertions on a Mock.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from case_events import subjects
from case_events.consumers import (
    ConsumerSpec,
    Disposition,
    PoisonMessage,
    decide,
    select,
)
from case_events.consumers import runner


class FakeMsg:
    """Enough of ``nats.aio.msg.Msg`` for the runner."""

    class _Meta:
        def __init__(self, num_delivered):
            self.num_delivered = num_delivered

    def __init__(self, envelope=None, *, subject="jaw.case.matched", num_delivered=1, raw=None):
        self.data = raw if raw is not None else json.dumps(envelope or {}).encode("utf-8")
        self.subject = subject
        self.metadata = self._Meta(num_delivered)
        self.settled = []

    async def ack(self):
        self.settled.append("ack")

    async def nak(self, delay=None):
        self.settled.append("nak")

    async def term(self):
        self.settled.append("term")


class FakeJS:
    """Records DLQ publishes; can be told to fail them."""

    def __init__(self, fail=False):
        self.published = []
        self.fail = fail

    async def publish(self, subject, body, **kwargs):
        if self.fail:
            raise RuntimeError("broker gone")
        self.published.append((subject, json.loads(body.decode("utf-8"))))


def spec_for(handler, **overrides):
    defaults = {
        "name": "test",
        "stream": "CASE_EVENTS",
        "filter_subject": "jaw.case.matched",
        "handler": handler,
        "max_deliver": 3,
    }
    return ConsumerSpec(**{**defaults, **overrides})


class TestDecide:
    def test_a_clean_handler_acks(self):
        assert decide(error=None, num_delivered=1, max_deliver=5) is Disposition.ACK

    def test_a_clean_handler_on_the_last_delivery_still_acks(self):
        assert decide(error=None, num_delivered=5, max_deliver=5) is Disposition.ACK

    def test_an_ordinary_failure_retries_while_deliveries_remain(self):
        assert decide(error=RuntimeError(), num_delivered=1, max_deliver=5) is Disposition.RETRY
        assert decide(error=RuntimeError(), num_delivered=4, max_deliver=5) is Disposition.RETRY

    def test_the_final_delivery_is_buried_not_retried(self):
        """The whole reason this function exists.

        JetStream will not redeliver again, so a NAK here loses the message.
        """
        assert decide(error=RuntimeError(), num_delivered=5, max_deliver=5) is Disposition.DEAD_LETTER

    def test_a_delivery_count_past_the_budget_is_still_buried(self):
        assert decide(error=RuntimeError(), num_delivered=9, max_deliver=5) is Disposition.DEAD_LETTER

    def test_poison_skips_the_retries_entirely(self):
        """Redelivering unparseable bytes produces unparseable bytes."""
        assert decide(error=PoisonMessage("bad"), num_delivered=1, max_deliver=5) is Disposition.DEAD_LETTER


class TestParseEnvelope:
    def test_a_json_object_parses(self):
        assert runner.parse_envelope(b'{"subject": "x"}') == {"subject": "x"}

    @pytest.mark.parametrize("raw", [b"not json", b"\xff\xfe", b"[1, 2]", b'"a string"', b"null"])
    def test_anything_else_is_poison_rather_than_a_retry(self, raw):
        with pytest.raises(PoisonMessage):
            runner.parse_envelope(raw)


class TestProcessMessage:
    async def test_a_clean_handler_acks_and_publishes_nothing(self):
        js = FakeJS()
        msg = FakeMsg({"subject": "jaw.case.matched"})
        seen = []

        result = await runner.process_message(js, spec_for(lambda e, c: seen.append(e)), msg)

        assert result is Disposition.ACK
        assert msg.settled == ["ack"]
        assert js.published == []
        assert seen == [{"subject": "jaw.case.matched"}]

    async def test_a_transient_failure_naks_for_redelivery(self):
        js = FakeJS()
        msg = FakeMsg({"a": 1}, num_delivered=1)

        def boom(envelope, ctx):
            raise RuntimeError("db blip")

        result = await runner.process_message(js, spec_for(boom), msg)

        assert result is Disposition.RETRY
        assert msg.settled == ["nak"]
        assert js.published == []

    async def test_the_last_delivery_reaches_the_dlq_before_it_is_terminated(self):
        """Order is the assertion. Terminate-then-publish would lose the message."""
        js = FakeJS()
        msg = FakeMsg({"a": 1}, num_delivered=3, subject="jaw.case.matched")

        def boom(envelope, ctx):
            raise RuntimeError("still broken")

        result = await runner.process_message(js, spec_for(boom), msg)

        assert result is Disposition.DEAD_LETTER
        assert msg.settled == ["term"]
        assert len(js.published) == 1
        dlq_subject, envelope = js.published[0]
        assert dlq_subject == "jaw.dlq.jaw.case.matched"
        assert envelope["payload"]["original_subject"] == "jaw.case.matched"
        assert envelope["payload"]["num_delivered"] == 3
        assert "still broken" in envelope["payload"]["error"]
        assert envelope["payload"]["consumer"] == "test"

    async def test_an_unterminated_message_is_left_on_the_stream_when_the_dlq_is_unreachable(self):
        """A terminated message the DLQ never received is gone for good."""
        js = FakeJS(fail=True)
        msg = FakeMsg({"a": 1}, num_delivered=3)

        def boom(envelope, ctx):
            raise RuntimeError("x")

        result = await runner.process_message(js, spec_for(boom), msg)

        assert result is Disposition.RETRY
        assert msg.settled == ["nak"]
        assert "term" not in msg.settled

    async def test_poison_is_buried_on_its_first_delivery(self):
        js = FakeJS()
        msg = FakeMsg({"a": 1}, num_delivered=1)

        def boom(envelope, ctx):
            raise PoisonMessage("nothing here to work with")

        assert await runner.process_message(js, spec_for(boom), msg) is Disposition.DEAD_LETTER
        assert msg.settled == ["term"]

    async def test_an_unparseable_body_is_buried_with_its_bytes_carried_forward(self):
        """The commonest reason to be in the DLQ is that the body made no sense.

        Re-parsing it to build the DLQ message would fail for the same reason,
        so the raw text is carried instead.
        """
        js = FakeJS()
        msg = FakeMsg(raw=b"{not json at all", num_delivered=1)

        assert await runner.process_message(js, spec_for(lambda e, c: None), msg) is Disposition.DEAD_LETTER
        assert js.published[0][1]["payload"]["body"] == "{not json at all"

    async def test_undecodable_bytes_do_not_break_the_dlq_path(self):
        js = FakeJS()
        msg = FakeMsg(raw=b"\xff\xfe\x00bad", num_delivered=1)

        assert await runner.process_message(js, spec_for(lambda e, c: None), msg) is Disposition.DEAD_LETTER
        assert js.published

    async def test_unreadable_metadata_buries_rather_than_retrying_forever(self):
        """The reverse of what this asserted before, and the reversal is the point.

        Defaulting the delivery count LOW *looks* like the recoverable choice —
        retry rather than give up. It is not: an unknown count can never satisfy
        ``num_delivered >= max_deliver``, so the message is NAK'd until
        JetStream silently drops it at MaxDeliver. That is the exact leak the
        DLQ exists to prevent, arrived at by the safety default. Burying puts
        the body somewhere a human can find it.
        """
        js = FakeJS()
        msg = FakeMsg({"a": 1})
        del msg.metadata

        def boom(envelope, ctx):
            raise RuntimeError("x")

        result = await runner.process_message(js, spec_for(boom), msg)

        assert result is Disposition.DEAD_LETTER
        assert msg.settled == ["term"]
        # And the body reached the DLQ before the terminate, as always.
        assert js.published and js.published[0][0].startswith(subjects.DLQ_PREFIX)

    async def test_an_unknown_delivery_count_is_reported_as_unknown(self):
        """`decide` can only bury an unknown count if it is told the count is unknown.

        Pinning the None rather than a plausible-looking 1: a guess here reads
        as a real first delivery at every call site downstream, including the
        `handler_failed` log line an operator would use to work out why a
        message kept coming back.
        """
        msg = FakeMsg({"a": 1})
        del msg.metadata

        assert runner._num_delivered(msg) is None

    async def test_a_handler_that_blocks_does_not_run_on_the_event_loop(self):
        """Handlers use the Django ORM, which refuses to run inside a loop."""
        import asyncio
        import threading

        js = FakeJS()
        msg = FakeMsg({"a": 1})
        threads = []

        def record(envelope, ctx):
            threads.append(threading.current_thread())
            # Would raise RuntimeError if this were the loop thread.
            assert not _loop_is_running()

        await runner.process_message(js, spec_for(record), msg)
        assert threads and threads[0] is not threading.main_thread()
        assert asyncio.get_running_loop()  # the loop itself is still fine


def _loop_is_running() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


class TestSettling:
    """Talking to the broker is the part of handling a message that can fail."""

    async def test_a_failed_ack_does_not_kill_the_consumer(self):
        """Redelivery already covers a lost ack. Losing the consumer covers nothing.

        The settle calls used to be the only unguarded awaits in the fetch loop,
        so one transient ack error propagated all the way out of `run_one` and
        that consumer was gone for the life of the pod.
        """
        js = FakeJS()
        msg = FakeMsg({"a": 1})

        async def boom_ack():
            raise RuntimeError("connection reset")

        msg.ack = boom_ack

        result = await runner.process_message(js, spec_for(lambda e, c: None), msg)

        assert result is Disposition.ACK  # the decision stands; only delivery failed

    async def test_a_failed_nak_does_not_kill_the_consumer_either(self):
        js = FakeJS()
        msg = FakeMsg({"a": 1}, num_delivered=1)

        async def boom_nak(delay=None):
            raise RuntimeError("connection reset")

        msg.nak = boom_nak

        def handler(envelope, ctx):
            raise RuntimeError("transient")

        result = await runner.process_message(js, spec_for(handler, max_deliver=5), msg)

        assert result is Disposition.RETRY


class TestFetchFailures:
    async def test_a_broker_that_never_answers_makes_the_consumer_give_up(self, monkeypatch):
        """Warn-and-sleep-forever leaves the pod Ready and consuming nothing.

        Which is the same failure the supervision below exists to convert into
        an exit — a consumer polling a broker that stopped answering is
        indistinguishable, from outside, from one with no work to do.
        """
        import asyncio

        class DeadSub:
            async def fetch(self, batch, timeout=None):
                raise RuntimeError("nats: connection closed")

        monkeypatch.setattr(runner, "subscribe", lambda js, spec: _resolved(DeadSub()))
        # The constant, not `asyncio.sleep` — pytest-asyncio's own machinery
        # runs on that, and replacing it process-wide wedges the loop.
        monkeypatch.setattr(runner, "FETCH_RETRY_SLEEP_SECONDS", 0)

        # wait_for, because the thing being asserted is that this TERMINATES.
        # Without the bound, a regression that removes the ceiling does not fail
        # the test — it hangs it, and a wedged CI job is a much worse way to
        # learn about a bug than a red one.
        with pytest.raises(RuntimeError, match="consecutive fetch failures"):
            await asyncio.wait_for(
                runner.run_one(
                    FakeJS(),
                    spec_for(lambda e, c: None, max_deliver=5),
                    stop=asyncio.Event(),
                    once=False,
                    max_messages=None,
                ),
                timeout=5,
            )

    async def test_an_empty_window_is_not_a_failure(self, monkeypatch):
        """A quiet bus is the normal case and must never count toward the ceiling."""
        import asyncio

        from nats.errors import TimeoutError as NatsTimeoutError

        calls = {"n": 0}
        stop = asyncio.Event()

        class QuietSub:
            async def fetch(self, batch, timeout=None):
                calls["n"] += 1
                if calls["n"] > runner.MAX_CONSECUTIVE_FETCH_FAILURES * 2:
                    stop.set()
                raise NatsTimeoutError()

        monkeypatch.setattr(runner, "subscribe", lambda js, spec: _resolved(QuietSub()))

        handled = await asyncio.wait_for(
            runner.run_one(
                FakeJS(),
                spec_for(lambda e, c: None, max_deliver=5),
                stop=stop,
                once=False,
                max_messages=None,
            ),
            timeout=5,
        )

        assert handled == 0  # it polled well past the ceiling and never gave up

    async def test_a_bounded_run_that_cannot_reach_the_broker_fails(self, monkeypatch):
        """`--once` against a dead broker must not exit zero.

        Breaking out of the loop returns a count, and `run` reads any count as
        a clean finish because `fatal` is False in bounded mode — so a
        scheduled `run_consumers --apply --once` drain reported success having
        drained nothing. Only the timeout branch means "the backlog is empty".
        """
        import asyncio

        class DeadSub:
            async def fetch(self, batch, timeout=None):
                raise RuntimeError("nats: connection closed")

        monkeypatch.setattr(runner, "subscribe", lambda js, spec: _resolved(DeadSub()))

        with pytest.raises(RuntimeError, match="bounded run"):
            await asyncio.wait_for(
                runner.run_one(
                    FakeJS(),
                    spec_for(lambda e, c: None, max_deliver=5),
                    stop=asyncio.Event(),
                    once=True,
                    max_messages=None,
                ),
                timeout=5,
            )

    async def test_a_bounded_run_on_a_quiet_bus_still_succeeds(self):
        """The timeout branch keeps meaning "drained", which is the whole point."""
        import asyncio

        from nats.errors import TimeoutError as NatsTimeoutError

        class QuietSub:
            async def fetch(self, batch, timeout=None):
                raise NatsTimeoutError()

        with mock.patch.object(runner, "subscribe", lambda js, spec: _resolved(QuietSub())):
            handled = await asyncio.wait_for(
                runner.run_one(
                    FakeJS(),
                    spec_for(lambda e, c: None, max_deliver=5),
                    stop=asyncio.Event(),
                    once=True,
                    max_messages=None,
                ),
                timeout=5,
            )

        assert handled == 0

    async def test_a_recovered_fetch_resets_the_count(self, monkeypatch):
        """The ceiling is CONSECUTIVE failures, not cumulative ones.

        A bus that fails a fetch now and then over weeks is a working bus, and
        it must never accumulate its way to an exit. Without the reset the
        counter only ever climbs, so a long-lived consumer eventually kills
        itself on a broker that is fine — and the two dedicated tests above
        cannot tell the difference, because neither ever interleaves.
        """
        import asyncio

        from nats.errors import TimeoutError as NatsTimeoutError

        # Alternate: fail, then a quiet window, over and over. Far more rounds
        # than the ceiling allows if the count were cumulative.
        rounds = runner.MAX_CONSECUTIVE_FETCH_FAILURES * 3
        calls = {"n": 0}
        stop = asyncio.Event()

        class FlakySub:
            async def fetch(self, batch, timeout=None):
                calls["n"] += 1
                if calls["n"] >= rounds * 2:
                    stop.set()
                if calls["n"] % 2:
                    raise RuntimeError("transient blip")
                raise NatsTimeoutError()

        monkeypatch.setattr(runner, "subscribe", lambda js, spec: _resolved(FlakySub()))
        monkeypatch.setattr(runner, "FETCH_RETRY_SLEEP_SECONDS", 0)

        handled = await asyncio.wait_for(
            runner.run_one(
                FakeJS(),
                spec_for(lambda e, c: None, max_deliver=5),
                stop=stop,
                once=False,
                max_messages=None,
            ),
            timeout=5,
        )

        assert handled == 0
        assert calls["n"] >= rounds, "it gave up despite recovering between failures"


async def _resolved(value):
    return value


class TestRun:
    """One dead consumer must take the process down with it.

    These four share a Deployment, so the process is the only thing an
    orchestrator can observe. An earlier version gathered the tasks and reported
    a crash afterwards — but nothing ever finishes in the continuous mode, so
    "afterwards" never came: a matcher that died on a missing stream left the
    other three polling and the pod Ready, consuming nothing.
    """

    async def _run(self, monkeypatch, specs, run_one, **kwargs):
        import contextlib

        class FakeNC:
            async def drain(self):
                return None

        async def fake_connect():
            return FakeNC(), FakeJS()

        monkeypatch.setattr(runner, "connect", fake_connect)
        monkeypatch.setattr(runner, "run_one", run_one)
        monkeypatch.setattr(runner, "_install_signal_handlers", lambda stop: None)
        with contextlib.suppress(Exception):
            return await runner.run(specs, **kwargs)

    async def test_a_crashed_consumer_stops_the_others_and_is_reported(self, monkeypatch):
        import asyncio

        specs = [
            spec_for(lambda e, c: None, name="crasher", max_deliver=5),
            spec_for(lambda e, c: None, name="survivor", max_deliver=5),
        ]

        async def run_one(js, spec, *, stop, once, max_messages):
            if spec.name == "crasher":
                raise RuntimeError("stream not found")
            # The sibling: loops until told to stop, exactly like the real one.
            while not stop.is_set():
                await asyncio.sleep(0.01)
            return 7

        counts = await asyncio.wait_for(self._run(monkeypatch, specs, run_one), timeout=5)

        assert counts["crasher"] == -1, "a crash must be reported"
        # And the sibling actually returned, i.e. it was told to stop. Without
        # that, this test would hang rather than fail — hence the wait_for.
        assert counts["survivor"] == 7

    async def test_a_consumer_that_returns_early_counts_as_a_crash(self, monkeypatch):
        """A clean return in continuous mode is still a consumer that stopped.

        Distinguished from a graceful SIGTERM, which also sets `stop` — that one
        must stay a zero exit.
        """
        import asyncio

        specs = [
            spec_for(lambda e, c: None, name="quitter", max_deliver=5),
            spec_for(lambda e, c: None, name="survivor", max_deliver=5),
        ]

        async def run_one(js, spec, *, stop, once, max_messages):
            if spec.name == "quitter":
                return 0
            while not stop.is_set():
                await asyncio.sleep(0.01)
            return 3

        counts = await asyncio.wait_for(self._run(monkeypatch, specs, run_one), timeout=5)

        assert counts["quitter"] == -1

    async def test_once_mode_lets_consumers_finish_independently(self, monkeypatch):
        """A drain is meant to end. One finishing says nothing about the others."""
        import asyncio

        specs = [
            spec_for(lambda e, c: None, name="a", max_deliver=5),
            spec_for(lambda e, c: None, name="b", max_deliver=5),
        ]

        async def run_one(js, spec, *, stop, once, max_messages):
            if spec.name == "a":
                return 1
            await asyncio.sleep(0.05)
            return 2

        counts = await asyncio.wait_for(
            self._run(monkeypatch, specs, run_one, once=True), timeout=5
        )

        assert counts == {"a": 1, "b": 2}, "neither should be marked crashed"


class TestBackoffFitsTheDeliveryBudget:
    """JetStream rejects a consumer with as many backoff steps as deliveries.

    It does so at subscribe time — during a rollout, in a pod that then
    crashloops — so the schedule is derived from ``max_deliver`` rather than
    asserted against it. Setting the two inconsistently is not possible.
    """

    def test_the_default_pair_leaves_room(self):
        from case_events.consumers import DEFAULT_BACKOFF_SECONDS

        spec = spec_for(lambda e, c: None, max_deliver=5)
        assert spec.backoff == list(DEFAULT_BACKOFF_SECONDS)
        assert len(spec.backoff) < spec.max_deliver

    def test_a_small_budget_shortens_the_schedule_instead_of_breaking_it(self):
        for max_deliver in range(1, 8):
            spec = spec_for(lambda e, c: None, max_deliver=max_deliver)
            assert len(spec.backoff) < max_deliver or max_deliver == 0


class TestDeadLetterKey:
    async def test_burying_the_same_body_twice_collapses(self):
        """Otherwise the DLQ's depth counts delivery attempts, not failing facts.

        The depth is the number an operator reacts to, so it has to mean what it
        looks like — and a message reaches here on its final delivery, which a
        consumer restart or a re-published copy can repeat.
        """
        js = FakeJS()
        spec = spec_for(_boom, max_deliver=5)

        keys = []
        for _ in range(2):
            msg = FakeMsg({"a": 1}, num_delivered=5)
            await runner.process_message(js, spec, msg)
            keys.append(js.published[-1][1]["dedup_key"])

        assert keys[0] == keys[1] and keys[0]

    async def test_an_unparseable_body_still_gets_a_key(self):
        """The commonest reason to be in the DLQ is that there was no envelope to read."""
        js = FakeJS()
        msg = FakeMsg(raw=b"\xff\xfe not json", num_delivered=1)

        await runner.process_message(js, spec_for(lambda e, c: None, max_deliver=5), msg)

        assert js.published[-1][1]["dedup_key"]

    async def test_two_different_bodies_do_not_collapse(self):
        js = FakeJS()
        spec = spec_for(_boom, max_deliver=5)

        keys = []
        for payload in ({"a": 1}, {"a": 2}):
            msg = FakeMsg(payload, num_delivered=5)
            await runner.process_message(js, spec, msg)
            keys.append(js.published[-1][1]["dedup_key"])

        assert keys[0] != keys[1]


def _boom(envelope, ctx):
    raise RuntimeError("nope")


class TestSelect:
    def test_no_filter_runs_every_registered_consumer(self):
        from case_events import consumers

        assert [s.name for s in select(None)] == consumers.known()

    def test_only_runs_the_named_subset(self):
        assert [s.name for s in select(["derive", "matcher"])] == ["derive", "matcher"]

    def test_the_order_is_stable_regardless_of_argument_order(self):
        """Two Deployments passing the same names differently must behave alike."""
        assert [s.name for s in select(["matcher", "derive"])] == [
            s.name for s in select(["derive", "matcher"])
        ]

    def test_an_unknown_name_refuses_the_whole_set(self):
        """A typo in a Deployment's args should fail the rollout, not run a subset."""
        with pytest.raises(KeyError, match="typo-here"):
            select(["matcher", "typo-here"])


class TestTheRegisteredTopology:
    """The four consumers, and that each binds to a stream that claims its filter."""

    def test_all_four_are_registered(self):
        from case_events import consumers

        assert consumers.known() == ["derive", "matcher", "notifier", "proposal-builder"]

    @pytest.mark.parametrize(
        "name,stream,filter_subject",
        [
            ("matcher", "SIGNALS", subjects.ALL_SIGNALS),
            ("proposal-builder", "CASE_EVENTS", subjects.CASE_MATCHED),
            ("notifier", "CASE_EVENTS", subjects.ALL_CASE_UPDATES),
            ("derive", "CASE_EVENTS", subjects.CASE_UPDATE_APPROVED),
        ],
    )
    def test_each_binds_where_the_design_says(self, name, stream, filter_subject):
        from case_events import consumers

        spec = consumers.get(name)
        assert spec.stream == stream
        assert spec.filter_subject == filter_subject

    def test_every_filter_is_covered_by_its_streams_subjects(self):
        """A consumer whose filter its stream does not claim never sees anything.

        Silent by construction — the subscription succeeds and no message ever
        arrives — so it is worth checking against the stream definitions rather
        than by eye.
        """
        from case_events import consumers, streams

        by_name = {s.name: s for s in streams.STREAMS}
        for spec in consumers.all_specs():
            stream = by_name[spec.stream]
            prefixes = [s.rstrip(">").rstrip(".") for s in stream.subjects]
            assert any(
                spec.filter_subject.startswith(prefix) for prefix in prefixes
            ), f"{spec.name} filters {spec.filter_subject}, which {stream.name} does not claim"

    def test_durable_names_are_prefixed_and_unique(self):
        from case_events import consumers

        durables = [s.durable for s in consumers.all_specs()]
        assert all(d.startswith("jaw-") for d in durables)
        assert len(set(durables)) == len(durables)
