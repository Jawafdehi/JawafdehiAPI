"""Shutdown drain noise must not look like a real NATS failure.

nats-py reports every async error through one callback whose default is
``logger.error("nats: encountered error", exc_info=ex)``. With the Sentry
logging integration attached that makes an issue per occurrence, and this
estate raises ``DrainTimeoutError`` on every pod shutdown whose drain does not
finish inside the client's 30s budget — 86 events across five ReplicaSet
generations of ``platform-bus-consumers``, once per rollout by construction.

Worse than the volume: all of it grouped under one title that named no
exception, so a stale credential or a broken subscription would land in the
same bucket and never be seen.
"""

import logging

import pytest

from case_events import nats_logging


class _FakeDrainTimeoutError(Exception):
    """Stands in for ``nats.errors.DrainTimeoutError``.

    Matched by class name on purpose — see the note in ``nats_logging`` — so a
    stand-in with the right name is the honest way to test the classifier.
    """


_FakeDrainTimeoutError.__name__ = "DrainTimeoutError"


class _FakeAuthError(Exception):
    pass


_FakeAuthError.__name__ = "AuthorizationError"


class TestClassification:
    def test_a_drain_timeout_is_shutdown_noise(self):
        assert nats_logging.is_shutdown_noise(_FakeDrainTimeoutError())

    def test_a_real_failure_is_not(self):
        assert not nats_logging.is_shutdown_noise(_FakeAuthError("bad creds"))

    def test_an_unrelated_exception_is_not(self):
        assert not nats_logging.is_shutdown_noise(ValueError("boom"))


class TestCallback:
    pytestmark = pytest.mark.asyncio(loop_scope="function")

    async def test_a_drain_timeout_logs_at_warning(self, caplog):
        with caplog.at_level(logging.DEBUG):
            await nats_logging.error_cb(_FakeDrainTimeoutError())
        record = caplog.records[-1]
        assert record.levelno == logging.WARNING

    async def test_a_real_error_still_logs_at_error(self, caplog):
        """The whole point is to keep this one loud."""
        with caplog.at_level(logging.DEBUG):
            await nats_logging.error_cb(_FakeAuthError("nats: authorization violation"))
        record = caplog.records[-1]
        assert record.levelno == logging.ERROR

    async def test_the_exception_class_rides_along(self, caplog):
        """So distinct failures group distinctly instead of collapsing."""
        with caplog.at_level(logging.DEBUG):
            await nats_logging.error_cb(_FakeAuthError("violation"))
        assert "AuthorizationError" in caplog.text

    async def test_an_exception_with_no_message_still_says_something(self, caplog):
        with caplog.at_level(logging.DEBUG):
            await nats_logging.error_cb(_FakeDrainTimeoutError())
        assert "DrainTimeoutError" in caplog.text


class TestTracebacksSurvive:
    """Found in review: the first version dropped `exc_info`.

    nats-py's default is `logger.error(..., exc_info=ex)`, and
    `configure_structlog()` carries `format_exc_info` specifically to render
    it. Without it a real failure reaches Sentry as a message-only event with
    no frames, so there is no way to tell which subscription or connection
    raised. Quieter must not mean thinner.
    """

    pytestmark = pytest.mark.asyncio(loop_scope="function")

    @staticmethod
    def _rendered(record):
        """structlog consumes `exc_info` and renders it into an `exception` key.

        So asserting on `record.exc_info` would pass vacuously (it is always
        None here); the traceback lands in the event dict instead.
        """
        return record.msg if isinstance(record.msg, dict) else {}

    async def test_a_real_error_keeps_its_traceback(self, caplog):
        try:
            raise _FakeAuthError("authorization violation")
        except _FakeAuthError as exc:
            with caplog.at_level(logging.DEBUG):
                await nats_logging.error_cb(exc)

        rendered = self._rendered(caplog.records[-1])
        assert "exception" in rendered, "the traceback must survive the callback"
        assert "Traceback" in rendered["exception"]
        assert "authorization violation" in rendered["exception"]
        # The rendered traceback shows the real class; `error_type` is what
        # carries the name used for grouping.
        assert rendered["error_type"] == "AuthorizationError"

    async def test_the_shutdown_path_keeps_it_too(self, caplog):
        """Downgraded to WARNING, not stripped of detail."""
        try:
            raise _FakeDrainTimeoutError("drain timed out")
        except _FakeDrainTimeoutError as exc:
            with caplog.at_level(logging.DEBUG):
                await nats_logging.error_cb(exc)

        record = caplog.records[-1]
        assert record.levelno == logging.WARNING
        assert "Traceback" in self._rendered(record).get("exception", "")
