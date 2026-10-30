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


#: The REAL class. Matching is by class where nats-py is importable, so a
#: stand-in with the right name is an impostor, not a stand-in — see
#: TestTheNameFallbackIsOnlyAFallback, which pins that it is treated as one.
from nats.errors import DrainTimeoutError as _RealDrainTimeoutError


class _FakeDrainTimeoutError(Exception):
    """An unrelated class that merely shares the name. Must NOT be downgraded."""


_FakeDrainTimeoutError.__name__ = "DrainTimeoutError"


class _FakeAuthError(Exception):
    pass


_FakeAuthError.__name__ = "AuthorizationError"


class TestClassification:
    def test_a_drain_timeout_is_shutdown_noise(self):
        assert nats_logging.is_shutdown_noise(_RealDrainTimeoutError())

    def test_a_real_failure_is_not(self):
        assert not nats_logging.is_shutdown_noise(_FakeAuthError("bad creds"))

    def test_an_unrelated_exception_is_not(self):
        assert not nats_logging.is_shutdown_noise(ValueError("boom"))


class TestCallback:
    pytestmark = pytest.mark.asyncio(loop_scope="function")

    async def test_a_drain_timeout_logs_at_warning(self, caplog):
        with caplog.at_level(logging.DEBUG):
            await nats_logging.error_cb(_RealDrainTimeoutError())
        record = caplog.records[-1]
        assert record.levelno == logging.WARNING

    async def test_a_real_error_still_logs_at_error(self, caplog):
        """The whole point is to keep this one loud."""
        with caplog.at_level(logging.DEBUG):
            await nats_logging.error_cb(_FakeAuthError("nats: authorization violation"))
        record = caplog.records[-1]
        assert record.levelno == logging.ERROR


class TestSentryGetsARealException:
    """The traceback must reach Sentry as an EXCEPTION, not as text.

    Round two of this PR passed `exc_info` to structlog, and the test here
    asserted on the rendered `exception` string — which passed while being
    cosmetic. structlog's `format_exc_info` consumes `exc_info` before the
    LogRecord is built, leaving `record.exc_info is None`, and Sentry's
    LoggingIntegration builds an exception event ONLY from `record.exc_info`.
    The result was a message-only issue with no type and no frames: strictly
    worse than the nats-py default it replaced.
    """

    pytestmark = pytest.mark.asyncio(loop_scope="function")

    async def test_the_record_carries_the_exception_itself(self, caplog):
        try:
            raise _FakeAuthError("authorization violation")
        except _FakeAuthError as exc:
            with caplog.at_level(logging.DEBUG):
                await nats_logging.error_cb(exc)

        record = caplog.records[-1]
        assert record.exc_info is not None, (
            "Sentry builds an exception event only from record.exc_info"
        )
        assert record.exc_info[0] is _FakeAuthError

    async def test_the_shutdown_path_keeps_the_exception_too(self, caplog):
        """Downgraded to WARNING, not stripped of detail."""
        try:
            raise _RealDrainTimeoutError("drain timed out")
        except _RealDrainTimeoutError as exc:
            with caplog.at_level(logging.DEBUG):
                await nats_logging.error_cb(exc)

        record = caplog.records[-1]
        assert record.levelno == logging.WARNING
        assert record.exc_info is not None
        assert record.exc_info[0] is _RealDrainTimeoutError

    async def test_the_message_names_the_class(self, caplog):
        with caplog.at_level(logging.DEBUG):
            await nats_logging.error_cb(_FakeAuthError("violation"))
        assert "AuthorizationError" in caplog.records[-1].getMessage()

    async def test_an_exception_with_no_message_still_says_something(self, caplog):
        with caplog.at_level(logging.DEBUG):
            await nats_logging.error_cb(_FakeDrainTimeoutError())
        assert "DrainTimeoutError" in caplog.records[-1].getMessage()


class TestRealClassesArePreferredOverNames:
    """Round four: name matching alone cuts both ways.

    It silently downgrades any unrelated exception that happens to be called
    `DrainTimeoutError`, and silently STOPS downgrading if nats-py renames one
    or raises a subclass — bringing the 86-events-per-rollout regression back
    at ERROR with nothing to say why.
    """

    def test_the_real_nats_classes_are_found(self):
        classes = nats_logging._shutdown_error_classes()
        assert classes, "nats-py is installed here, so these must resolve"
        assert {c.__name__ for c in classes} <= nats_logging._SHUTDOWN_ERROR_NAMES

    def test_a_real_drain_timeout_is_matched(self):
        from nats import errors

        assert nats_logging.is_shutdown_noise(errors.DrainTimeoutError())

    def test_a_subclass_is_matched_too(self):
        """Name matching alone would miss this."""
        from nats import errors

        class _Subclass(errors.DrainTimeoutError):
            pass

        assert nats_logging.is_shutdown_noise(_Subclass())

    def test_a_real_auth_error_is_not_matched(self):
        from nats import errors

        assert not nats_logging.is_shutdown_noise(errors.AuthorizationError())


class TestTheMessageIsNeverEmpty:
    """`exc or type(exc).__name__` was dead code — instances are truthy.

    The exceptions that dominate this path (anyio's ClosedResourceError,
    EndOfStream, and nats' own argument-less errors) all have an empty
    `str()`, so the intended fallback never fired.
    """

    pytestmark = pytest.mark.asyncio(loop_scope="function")

    async def test_an_exception_with_an_empty_str_still_names_itself(self, caplog):
        """anyio's ClosedResourceError/EndOfStream are exactly this shape.

        Note `nats.errors.DrainTimeoutError` is NOT — it carries its own
        message — so testing with that one would pass either way.
        """
        import anyio

        assert str(anyio.ClosedResourceError()) == "", "premise of this test"

        with caplog.at_level(logging.DEBUG):
            await nats_logging.error_cb(anyio.ClosedResourceError())
        message = caplog.records[-1].getMessage()
        assert message.endswith("ClosedResourceError")
        assert not message.endswith(": ")


class TestTheNameFallbackIsOnlyAFallback:
    """Round five: the fallback ran unconditionally, so class matching bought
    nothing. An unrelated exception merely NAMED `DrainTimeoutError` was still
    downgraded to WARNING and kept out of Sentry — the precise risk the class
    lookup was added to remove."""

    def test_an_impostor_is_not_downgraded(self):
        assert nats_logging._shutdown_error_classes(), "premise: classes resolve"
        assert not nats_logging.is_shutdown_noise(_FakeDrainTimeoutError())

    def test_the_real_one_still_is(self):
        from nats import errors

        assert nats_logging.is_shutdown_noise(errors.DrainTimeoutError())

    def test_the_classes_are_resolved_once(self):
        assert nats_logging._shutdown_error_classes() is (
            nats_logging._shutdown_error_classes()
        )
