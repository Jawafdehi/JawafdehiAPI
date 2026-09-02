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

    @pytest.mark.parametrize(
        "message",
        [
            "stop_reason: max_tokens",
            "stop_reason=max_tokens",
            '{"stop_reason":"max_tokens"}',
            "stop reason: max_tokens",
        ],
    )
    def test_an_explicit_stop_reason_is_read_as_exhaustion(self, message):
        assert is_exhaustion(RuntimeError(message))

    def test_the_cli_output_cap_is_read_as_exhaustion(self):
        """The verbatim production string, from the review judge on 2026-09-02.

        It names neither a turn limit nor a stop reason, so both matchers above
        miss it — which is how a truncated grade came to be reported as an
        unreachable judge and dead-lettered a whole review.
        """
        assert is_exhaustion(
            RuntimeError(
                "claude_cli failed (rc=1): API Error: Claude's response exceeded "
                "the 900 output token maximum. To configure this behavior, set "
                "the CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable."
            )
        )

    @pytest.mark.parametrize("cap", ["1", "900", "4000", "32000"])
    def test_the_output_cap_matches_at_any_budget(self, cap):
        assert is_exhaustion(f"response exceeded the {cap} output token maximum")

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

    @pytest.mark.parametrize(
        "message",
        [
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS must be a positive integer",
            "unset CLAUDE_CODE_MAX_OUTPUT_TOKENS to remove the output token maximum",
            "invalid output token maximum",
        ],
    )
    def test_an_output_cap_CONFIGURATION_error_is_not_exhaustion(self, message):
        """The cap's own error message recommends setting the env var, so matching
        on the var name — or on bare "output token maximum" — would read a
        complaint ABOUT the setting as a generation that overflowed it. Requiring
        the digits keeps the two apart."""
        assert not is_exhaustion(RuntimeError(message))

    @pytest.mark.parametrize(
        "message",
        [
            "invalid max_tokens value",
            "max_tokens: field required",
            "max_tokens must be >= 1, got 0",
            "unknown parameter: max_tokens",
        ],
    )
    def test_a_max_tokens_CONFIGURATION_error_is_not_exhaustion(self, message):
        """The bare substring "max_tokens" was the original marker, and it made
        every one of these look like an exhausted generation. They are bugs: an
        escalated retry pays several times over to fail in exactly the same way,
        which is the trap this module exists to avoid."""
        assert not is_exhaustion(RuntimeError(message))

    def test_a_timeout_is_not_exhaustion(self):
        # Tempting to treat as "it needed more room", but a bigger budget makes a
        # timeout MORE likely, not less: the model has more it is allowed to emit.
        assert not is_exhaustion(TimeoutError("timed out after 120s"))
