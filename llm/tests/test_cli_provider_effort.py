# SPDX-License-Identifier: LicenseRef-Hippocratic-3.0
"""The per-call `effort` on a plain `claude -p` call.

The CLI caps reasoning and answer together, so a caller with a tight output
budget asks to reason less for that one call without changing every other stage.
"""

from unittest import mock

import pytest
from django.test import override_settings

from llm import invoke
from llm.providers.cli import ClaudeCliProvider

RESULT = '{"type":"result","subtype":"success","result":"{}","usage":{}}'


def argv_for(effort=None, **settings_overrides):
    provider = ClaudeCliProvider()
    with override_settings(**settings_overrides):
        with mock.patch.object(ClaudeCliProvider, "_run", return_value=RESULT) as run:
            provider.invoke_text("sys", "content", 2000, "claude-opus-4-8", "premium", effort=effort)
    return run.call_args.args[0]


def effort(argv):
    return argv[argv.index("--effort") + 1] if "--effort" in argv else None


def test_no_effort_anywhere_leaves_the_cli_default():
    assert effort(argv_for(CLAUDE_CLI_EFFORT="")) is None


def test_the_setting_applies_when_the_call_names_none():
    assert effort(argv_for(CLAUDE_CLI_EFFORT="high")) == "high"


def test_the_call_overrides_the_setting():
    assert effort(argv_for("low", CLAUDE_CLI_EFFORT="high")) == "low"


def test_an_unknown_level_is_refused_before_the_cli_runs():
    with pytest.raises(ValueError, match="effort must be one of"):
        argv_for("none")


def test_invoke_text_passes_effort_only_when_given():
    calls = []

    class Fake:
        def model_for_tier(self, tier):
            return "m"

        def invoke_text(self, *args, **kwargs):
            calls.append(kwargs)
            return "{}"

    with mock.patch.object(invoke.routing, "provider_for_tier", return_value=Fake()):
        invoke.invoke_text("s", "c", 10)
        invoke.invoke_text("s", "c", 10, effort="low")
    assert calls == [{}, {"effort": "low"}]
