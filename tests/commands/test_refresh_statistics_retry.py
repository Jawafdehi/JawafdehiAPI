"""Retry + primary-fallback behaviour of the ``refresh_statistics`` command.

The nightly snapshot job routes its heavy reads onto the NGM read replica to keep
the counts off the primary. A hot standby can cancel a multi-second aggregation
mid-flight ("canceling statement due to conflict with recovery") when WAL replay
must remove row versions the query is still reading — a transient error that was
crashing the CronJob (and paging Sentry). The command must retry, and on its
FINAL attempt read from the primary (which never self-conflicts) so the snapshot
still refreshes even under continuous replay pressure.

These tests mock the aggregation itself, so they need no database — they assert
the retry count, the backoff sleeps, and (the crux) that the last attempt runs
with replica routing OFF.
"""

from unittest import mock

import pytest
from django.core.management import call_command
from django.db import OperationalError

from config.db_router import _reads_use_replica

COMMAND = "cases.management.commands.refresh_statistics"


def _run_with(side_effects, monkeypatch):
    """Invoke the command with ``refresh_statistics`` yielding ``side_effects``.

    Records the live replica-routing flag at each attempt and the backoff sleeps.
    Each side effect is either an exception instance (raised) or a return value.
    """
    seen_replica_flags = []
    sleeps = []

    def fake_refresh():
        seen_replica_flags.append(_reads_use_replica())
        effect = side_effects[len(seen_replica_flags) - 1]
        if isinstance(effect, Exception):
            raise effect
        return effect

    monkeypatch.setattr(f"{COMMAND}.time.sleep", lambda s: sleeps.append(s))
    with mock.patch(f"{COMMAND}.refresh_statistics", side_effect=fake_refresh):
        call_command("refresh_statistics")
    return seen_replica_flags, sleeps


def test_succeeds_first_try_uses_replica(monkeypatch):
    flags, sleeps = _run_with([{"last_updated": "now"}], monkeypatch)
    assert flags == [True]  # single attempt, on the replica
    assert sleeps == []  # no retry, no backoff


def test_retries_then_succeeds_still_on_replica(monkeypatch):
    flags, sleeps = _run_with(
        [OperationalError("conflict with recovery"), {"last_updated": "now"}],
        monkeypatch,
    )
    assert flags == [True, True]  # attempts 1 and 2 are both < MAX → replica
    assert sleeps == [2]  # one exponential backoff before the retry


def test_final_attempt_falls_back_to_primary(monkeypatch):
    # Fails on the replica twice; only the 3rd (final) attempt, on the PRIMARY,
    # succeeds — proving the fallback is what lets the job complete.
    flags, sleeps = _run_with(
        [
            OperationalError("conflict with recovery"),
            OperationalError("conflict with recovery"),
            {"last_updated": "now"},
        ],
        monkeypatch,
    )
    assert flags == [True, True, False]  # last attempt reads the primary
    assert sleeps == [2, 4]  # exponential backoff between attempts


def test_persistent_failure_propagates_after_max_attempts(monkeypatch):
    with pytest.raises(OperationalError):
        _run_with([OperationalError("boom")] * 3, monkeypatch)


def test_replica_routing_is_reset_after_run(monkeypatch):
    _run_with([{"last_updated": "now"}], monkeypatch)
    # The command's finally-block must always clear the flag so it never leaks
    # into a later in-process caller.
    assert _reads_use_replica() is False
