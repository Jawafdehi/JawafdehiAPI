# SPDX-License-Identifier: Hippocratic-3.0
"""An empty result inside a SUCCESS envelope.

`_finalize` validated that `result` EXISTS and never that it said anything. So
`result: ""` passed every check, `invoke_text` returned "", and the failure surfaced
two layers away in `llm.invoke.invoke_json` as::

    json.JSONDecodeError: Expecting value: line 1 column 1 (char 0)

— a message naming a column in a document that does not exist, raised from a call
site that never mentions the model. Seen in production 2026-08-04 on
`case_proposal.intent` job 2876, which then succeeded on retry.

Retry was and remains the right remedy. These pin the MESSAGE, not the behaviour.
"""

import json
from unittest import mock

import pytest
from django.test import override_settings

from llm.providers.cli import ClaudeCliProvider


def envelope(**overrides):
    return json.dumps({"type": "result", "subtype": "success", "result": "{}", **overrides})


def invoke(raw):
    provider = ClaudeCliProvider()
    with mock.patch.object(ClaudeCliProvider, "_run", return_value=raw):
        return provider.invoke_text("sys", "content", 2000, "claude-opus-4-8", "premium")


class TestAnEmptyResultIsNamed:
    @pytest.mark.parametrize("empty", ["", "   ", "\n\n", "```\n```"])
    def test_an_empty_result_raises_instead_of_returning_nothing(self, empty):
        """Whitespace and an empty code fence count as empty: `strip_code_fence`
        turns "```\\n```" into "", which reaches the JSON decoder identically."""
        with pytest.raises(RuntimeError, match="empty result"):
            invoke(envelope(result=empty))

    def test_the_error_does_not_mention_a_json_column(self):
        """The regression in words. The old failure said "line 1 column 1 (char 0)",
        which sent two investigations looking at the prompt's JSON."""
        with pytest.raises(RuntimeError) as caught:
            invoke(envelope(result=""))
        assert "column 1" not in str(caught.value)

    def test_the_error_carries_the_envelopes_own_numbers(self):
        """`llm.exhaustion` records that telling the two `error_max_turns` causes
        apart "would need num_turns from the CLI's result envelope", which "no
        caller can currently reach". This puts it in the message: a call that ended
        at num_turns=1 with tokens to spare was out of TURNS, not tokens."""
        with pytest.raises(RuntimeError) as caught:
            invoke(envelope(result="", num_turns=1, duration_ms=812, usage={"output_tokens": 0}))

        message = str(caught.value)
        assert "num_turns=1" in message
        assert "output_tokens=0" in message
        assert "subtype='success'" in message

    def test_a_normal_result_is_untouched(self):
        assert invoke(envelope(result='{"ok": true}')) == '{"ok": true}'

    def test_an_error_envelope_also_reports_the_numbers(self):
        """The `is_error` branch is where a real `error_max_turns` lands, and it is
        the case the num_turns question was actually asked about."""
        raw = json.dumps(
            {"type": "result", "subtype": "error_max_turns", "is_error": True,
             "result": "Reached maximum number of turns (1)", "num_turns": 1}
        )
        with pytest.raises(RuntimeError) as caught:
            invoke(raw)
        assert "num_turns=1" in str(caught.value)
        assert "subtype='error_max_turns'" in str(caught.value)


class TestItStaysOutOfTheEscalationPath:
    def test_an_empty_result_is_not_treated_as_exhaustion(self):
        """A bigger budget cannot help a model that returned nothing, and
        escalating pays roughly four times over to discover that. The retry that
        recovered job 2876 was a plain one, which is correct."""
        from llm.exhaustion import is_exhaustion

        with pytest.raises(RuntimeError) as caught:
            invoke(envelope(result=""))

        assert not is_exhaustion(caught.value), (
            "an empty result must not trigger the escalated-budget retry"
        )

    @override_settings(CLAUDE_CLI_MAX_TURNS=3)
    def test_a_real_turn_exhaustion_still_is(self):
        """The converse, so the test above cannot pass by `is_exhaustion` being
        broken outright."""
        from llm.exhaustion import is_exhaustion

        raw = json.dumps(
            {"type": "result", "subtype": "error_max_turns", "is_error": True,
             "result": "Reached maximum number of turns (3)", "num_turns": 3}
        )
        with pytest.raises(RuntimeError) as caught:
            invoke(raw)
        assert is_exhaustion(caught.value)
