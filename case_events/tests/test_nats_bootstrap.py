# SPDX-License-Identifier: Hippocratic-3.0
"""Tests for `manage.py nats_bootstrap`.

The property worth protecting is the inverse of the publisher's: this command
must fail LOUDLY. A silent failure means every later publish is dropped by a
broker with nowhere to put it, and the only symptom is a publish warning far
away from the cause.
"""

from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from case_events import streams


class TestDryRun:
    def test_lists_every_stream_without_connecting(self, capsys):
        with mock.patch("case_events.management.commands.nats_bootstrap._bootstrap") as boot:
            call_command("nats_bootstrap", "--dry-run")
        boot.assert_not_called()

        out = capsys.readouterr().out
        for spec in streams.STREAMS:
            assert spec.name in out
            assert spec.subjects[0] in out

    @override_settings(NATS_URL="")
    def test_dry_run_works_without_a_broker_configured(self, capsys):
        # Useful in CI and dev, where NATS_URL is deliberately unset.
        call_command("nats_bootstrap", "--dry-run")
        assert "SIGNALS" in capsys.readouterr().out


class TestFailsLoudly:
    @override_settings(NATS_URL="")
    def test_refuses_to_run_with_no_broker_configured(self):
        with pytest.raises(CommandError, match="NATS_URL is not set"):
            call_command("nats_bootstrap")

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_a_connection_failure_is_a_command_error_not_a_traceback(self):
        with mock.patch(
            "case_events.management.commands.nats_bootstrap._bootstrap",
            side_effect=OSError("connection refused"),
        ):
            with pytest.raises(CommandError, match="Could not assert streams"):
                call_command("nats_bootstrap")

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_the_error_names_the_exception_type(self):
        # `str(TimeoutError())` is empty, so the type is the only useful part —
        # the same trap the bus's connect logging had.
        with mock.patch(
            "case_events.management.commands.nats_bootstrap._bootstrap",
            side_effect=TimeoutError(),
        ):
            with pytest.raises(CommandError, match="TimeoutError"):
                call_command("nats_bootstrap")


class TestSuccess:
    @override_settings(NATS_URL="nats://localhost:4222")
    def test_reports_the_streams_it_asserted(self, capsys):
        with mock.patch(
            "case_events.management.commands.nats_bootstrap._bootstrap",
            return_value=["SIGNALS", "CASE_EVENTS", "DLQ"],
        ):
            call_command("nats_bootstrap")
        assert "Asserted 3 streams" in capsys.readouterr().out


class TestThePublisherNoLongerAssertsStreams:
    """The reason this command exists.

    Asserting from the publisher's connect path required every web process to
    hold JetStream stream-CREATE rights. This pins the split so it cannot
    silently regress the next time someone touches _connect.
    """

    def test_connect_does_not_import_or_call_ensure_streams(self):
        import inspect

        from case_events import bus

        source = inspect.getsource(bus._Bus._connect)
        assert "ensure_streams" not in source, (
            "The publisher must not assert stream topology — that needs "
            "stream-CREATE rights on the broker. Use manage.py nats_bootstrap."
        )
