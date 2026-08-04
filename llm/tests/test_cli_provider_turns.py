# SPDX-License-Identifier: Hippocratic-3.0
"""The turn cap on a plain `claude -p` call.

`--max-turns 1` was a hardcoded literal with no test, and it cost two wrong
diagnoses: a long answer asks to continue, the cap refuses, and the call aborts
as `error_max_turns` — which reads like an exhausted token budget. These pin the
flag so it cannot silently return to 1, and pin the arithmetic that stops a
misconfigured value from being worse than the bug.
"""

from unittest import mock

import pytest
from django.test import override_settings

from llm.providers.cli import ClaudeCliProvider

RESULT = '{"type":"result","subtype":"success","result":"{}","usage":{}}'


def argv_for(**settings_overrides):
    """Capture the argv the provider would run, without running anything."""
    provider = ClaudeCliProvider()
    with override_settings(**settings_overrides):
        with mock.patch.object(ClaudeCliProvider, "_run", return_value=RESULT) as run:
            provider.invoke_text("sys", "content", 2000, "claude-opus-4-8", "premium")
    return run.call_args.args[0]


def turns(argv):
    return argv[argv.index("--max-turns") + 1]


class TestTheTurnCap:
    def test_it_defaults_to_more_than_one_turn(self):
        """The whole fix. At 1, an answer that needs a continuation is discarded.

        Measured on case_proposal.intent: 3/5 at one turn, 5/5 at three.
        """
        assert int(turns(argv_for())) >= 2

    def test_it_is_read_from_settings(self):
        assert turns(argv_for(CLAUDE_CLI_MAX_TURNS=7)) == "7"

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_non_positive_setting_cannot_produce_an_unrunnable_call(self, bad):
        """`--max-turns 0` would reject every call outright. Floor it at 1 rather
        than fail the process: a misconfigured env var should degrade to the old
        behaviour, not take the worker down."""
        assert turns(argv_for(CLAUDE_CLI_MAX_TURNS=bad)) == "1"

    def test_tools_stay_disabled(self):
        """Raising the cap must not hand the model tools. More turns is about
        letting one answer finish, not about letting it act."""
        argv = argv_for()
        assert argv[argv.index("--allowedTools") + 1] == ""
        assert "--permission-mode" not in argv
