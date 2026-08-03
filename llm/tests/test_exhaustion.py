# SPDX-License-Identifier: Hippocratic-3.0
"""Recognising an exhausted token budget.

The point of these is the NEGATIVE cases. A helper that says yes too often does
not fail a test — it silently quadruples the bill on every unrelated error, which
is exactly the trap the narrowness is for.
"""

import pytest

from llm.exhaustion import EXHAUSTION_MARKERS, is_exhaustion


class TestFailuresThatMeanOutOfRoom:
    def test_the_cli_turn_limit_is_read_as_exhaustion(self):
        # How it actually presents: the budget is the cause, the turn limit is
        # only the messenger. This exact string came off a production job.
        assert is_exhaustion(
            RuntimeError('claude_cli failed (rc=1): "subtype":"error_max_turns"')
        )

    def test_the_prose_form_is_read_as_exhaustion(self):
        assert is_exhaustion(RuntimeError("Reached maximum number of turns (1)"))

    def test_an_explicit_token_cap_is_read_as_exhaustion(self):
        assert is_exhaustion(RuntimeError("stop_reason: max_tokens"))

    def test_matching_ignores_case(self):
        assert is_exhaustion(RuntimeError("ERROR_MAX_TURNS"))

    def test_a_bare_message_works_as_well_as_an_exception(self):
        # Callers that have already unwrapped the error should not have to re-wrap.
        assert is_exhaustion("error_max_turns")

    @pytest.mark.parametrize("marker", EXHAUSTION_MARKERS)
    def test_every_advertised_marker_is_actually_matched(self, marker):
        # Guards against a marker being added to the tuple in a form the matcher
        # cannot see — e.g. with different casing or surrounding punctuation.
        assert is_exhaustion(RuntimeError(f"upstream said: {marker}"))


class TestFailuresThatMustNotBeRetriedAtFourTimesTheCost:
    @pytest.mark.parametrize(
        "message",
        [
            "convert failed: no converter for application/zip",
            "no order url on case",
            "401 Unauthorized",
            "403 Forbidden: subscription required",
            "Expecting value: line 1 column 1 (char 0)",
            "Connection reset by peer",
            "model produced a list, not an object",
            "",
        ],
    )
    def test_an_unrelated_failure_is_not_exhaustion(self, message):
        assert not is_exhaustion(RuntimeError(message))

    def test_a_timeout_is_not_exhaustion(self):
        # Tempting to treat as "it needed more room", but a bigger budget makes a
        # timeout MORE likely, not less: the model has more it is allowed to emit.
        assert not is_exhaustion(TimeoutError("timed out after 120s"))
