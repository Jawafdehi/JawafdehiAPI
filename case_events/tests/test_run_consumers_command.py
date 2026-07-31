# SPDX-License-Identifier: Hippocratic-3.0
"""`manage.py run_consumers`.

Read-only by default, and that default is the point: the bare command tells you
what WOULD run without a broker, credentials, or a cluster — which is how you
check a Deployment's args before the Deployment exists.
"""

from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from case_events import consumers


class TestReadOnlyByDefault:
    def test_it_reports_every_consumer_without_connecting(self, capsys):
        with mock.patch("case_events.consumers.runner.run") as run:
            call_command("run_consumers")
        run.assert_not_called()

        out = capsys.readouterr().out
        for spec in consumers.all_specs():
            assert spec.name in out
            assert spec.durable in out
            assert spec.stream in out
            assert spec.filter_subject in out

    @override_settings(NATS_URL="")
    def test_the_report_works_with_no_broker_configured(self, capsys):
        call_command("run_consumers")
        assert "matcher" in capsys.readouterr().out

    def test_it_says_the_streams_must_exist_first(self, capsys):
        """The one ordering mistake that produces a silently idle consumer."""
        call_command("run_consumers")
        assert "nats_bootstrap" in capsys.readouterr().out

    def test_only_narrows_the_report(self, capsys):
        call_command("run_consumers", "--only", "derive")
        out = capsys.readouterr().out
        assert "derive" in out
        assert "matcher" not in out


class TestOnly:
    def test_an_unknown_name_is_a_command_error_listing_the_real_ones(self):
        with pytest.raises(CommandError) as exc:
            call_command("run_consumers", "--only", "matchr")
        assert "matchr" in str(exc.value)
        assert "matcher" in str(exc.value)

    def test_one_bad_name_refuses_the_whole_set(self):
        """A typo in a Deployment's args must fail the rollout, not run a subset."""
        with mock.patch("case_events.consumers.runner.run") as run:
            with pytest.raises(CommandError):
                call_command("run_consumers", "--apply", "--only", "matcher", "nope")
        run.assert_not_called()

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_apply_runs_exactly_the_named_consumers(self):
        with mock.patch("case_events.consumers.runner.run", return_value={"derive": 0}) as run:
            call_command("run_consumers", "--apply", "--only", "derive")
        (specs,) = run.call_args.args
        assert [s.name for s in specs] == ["derive"]


class TestApply:
    @override_settings(NATS_URL="")
    def test_it_refuses_to_run_with_no_broker(self):
        """Consuming has no meaningful no-op — an idle consumer looks healthy."""
        with pytest.raises(CommandError, match="NATS_URL is not set"):
            call_command("run_consumers", "--apply")

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_it_forwards_once_and_max_messages(self):
        with mock.patch("case_events.consumers.runner.run", return_value={}) as run:
            call_command("run_consumers", "--apply", "--once", "--max-messages", "3")
        assert run.call_args.kwargs == {"once": True, "max_messages": 3}

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_it_reports_what_each_consumer_handled(self, capsys):
        with mock.patch("case_events.consumers.runner.run", return_value={"matcher": 7}):
            call_command("run_consumers", "--apply", "--once")
        assert "matcher: handled 7" in capsys.readouterr().out

    @override_settings(NATS_URL="nats://localhost:4222")
    def test_a_crashed_consumer_fails_the_command(self):
        """Otherwise a half-dead consumer set exits 0 and nothing restarts it."""
        with mock.patch("case_events.consumers.runner.run", return_value={"matcher": 3, "derive": -1}):
            with pytest.raises(CommandError, match="derive"):
                call_command("run_consumers", "--apply", "--once")
