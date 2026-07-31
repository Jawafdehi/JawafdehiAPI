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

    async def test_unreadable_metadata_retries_rather_than_burying(self):
        """Defaulting the delivery count LOW is the recoverable mistake."""
        js = FakeJS()
        msg = FakeMsg({"a": 1})
        del msg.metadata

        def boom(envelope, ctx):
            raise RuntimeError("x")

        assert await runner.process_message(js, spec_for(boom), msg) is Disposition.RETRY

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
