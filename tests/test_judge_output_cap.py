# SPDX-License-Identifier: Hippocratic-3.0
"""A rule whose grade does not fit its output budget.

The production shape (2026-09-02): nine rules, a batch reply that truncated, and
three trailing rules — `description_summarises_case`, `tonal_neutrality`,
`gap_honesty` — re-graded individually on `invoke_json`'s 900-token default. On a
CLI provider that budget becomes CLAUDE_CODE_MAX_OUTPUT_TOKENS, which caps
reasoning as well as the answer, so each rescue call spent the lot thinking and
the CLI aborted rc=1. Two things then went wrong, and both are tested here:

* nothing retried at a larger budget — `llm.exhaustion.is_exhaustion` did not
  recognise the CLI's wording, and no caller in `review/` consulted it anyway;
* the truncation was classified as a TRANSPORT failure, so a judge that had
  answered every single time was reported unreachable and the whole review was
  dead-lettered over three ungraded rules.

The salvage half already worked and is covered by `test_judge_batch_robustness`:
a truncated batch keeps its leading rules. This file is about the rescue.
"""

from unittest import mock

import pytest

from review import judge

OUTPUT_CAP_ERROR = (
    "claude_cli failed (rc=1): API Error: Claude's response exceeded the 900 "
    "output token maximum. To configure this behavior, set the "
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS environment variable."
)


def _rule(key, *, is_gate=False):
    return {
        "key": key,
        "title": key,
        "description": "d",
        "good_examples": "",
        "bad_examples": "",
        "is_gate": is_gate,
    }


@pytest.fixture
def cli(settings):
    settings.REVIEW_LLM_PROVIDER_PREMIUM = "claude_cli"
    settings.REVIEW_LLM_PROVIDER_CHEAP = "claude_cli"
    settings.REVIEW_RULE_BATCH_SIZE = 1  # per-rule path, one call per rule
    return settings


def _judge(rules, fake):
    with mock.patch.object(judge, "invoke_json", side_effect=fake):
        return judge.judge_rules("case", "excerpts", "label", rules, n_samples=1)


class TestItRetriesAtALargerBudget:
    def test_a_rule_that_overflows_is_regraded_with_more_room(self, cli):
        budgets = []

        def fake(system, content, max_tokens=900, tier="premium", usage=None):
            if max_tokens == 300:
                return {"narrative": "n"}
            budgets.append(max_tokens)
            if max_tokens < 4000:
                raise RuntimeError(OUTPUT_CAP_ERROR)
            return {"score": 82, "rationale": "graded once it had room to think"}

        judged = _judge([_rule("tonal_neutrality")], fake)

        assert budgets == [900, 4000], "expected one escalation, smallest first"
        assert judged["tonal_neutrality"]["samples"] == [82]

    def test_a_rule_that_fits_costs_exactly_one_call(self, cli):
        """The escalation must be free for the rules that were never the problem."""
        budgets = []

        def fake(system, content, max_tokens=900, tier="premium", usage=None):
            if max_tokens == 300:
                return {"narrative": "n"}
            budgets.append(max_tokens)
            return {"score": 91, "rationale": "fits"}

        judged = _judge([_rule("routine")], fake)

        assert budgets == [900]
        assert judged["routine"]["samples"] == [91]

    def test_the_rungs_are_configurable(self, cli):
        cli.REVIEW_RULE_MAX_TOKENS = 1200
        cli.REVIEW_RULE_MAX_TOKENS_RETRY = 9000
        budgets = []

        def fake(system, content, max_tokens=900, tier="premium", usage=None):
            if max_tokens == 300:
                return {"narrative": "n"}
            budgets.append(max_tokens)
            if max_tokens < 9000:
                raise RuntimeError(OUTPUT_CAP_ERROR)
            return {"score": 70, "rationale": "ok"}

        _judge([_rule("gap_honesty")], fake)
        assert budgets == [1200, 9000]

    def test_a_failure_that_more_room_cannot_fix_is_not_retried(self, cli):
        """A 429 or a dead credential must still fail on the first call.

        Escalating those buys a dearer call to fail identically — the trap
        `llm/exhaustion.py` exists to prevent.
        """
        budgets = []

        def fake(system, content, max_tokens=900, tier="premium", usage=None):
            if max_tokens == 300:
                return {"narrative": "n"}
            budgets.append(max_tokens)
            raise RuntimeError("claude_cli failed (rc=1): api_error_status 429")

        with pytest.raises(judge.JudgeUnavailable):
            _judge([_rule("hard_gate", is_gate=True)], fake)

        assert budgets == [900], "a 429 must not buy a second, larger call"


class TestItIsNotReportedAsUnreachable:
    def test_an_exhausted_rule_degrades_instead_of_dead_lettering(self, cli):
        """Every call LANDED and was truncated. That is ungraded, not unavailable.

        Reporting it as unavailable is what turned three overflowing rules into a
        failed job for the entire review.
        """

        def fake(system, content, max_tokens=900, tier="premium", usage=None):
            if max_tokens == 300:
                return {"narrative": "n"}
            if "fits" in (content if isinstance(content, str) else content[-1]["text"]):
                return {"score": 88, "rationale": "fine"}
            raise RuntimeError(OUTPUT_CAP_ERROR)  # overflows at every budget

        judged = _judge(
            [_rule("fits"), _rule("description_summarises_case", is_gate=True)], fake
        )

        # The overflowing gate rule is ungraded with no fabricated sample, so the
        # scorer can tell it apart from a real 50 — and the rest still scores.
        assert judged["description_summarises_case"]["samples"] == []
        assert judged["description_summarises_case"]["mean"] == 50.0
        assert judged["fits"]["samples"] == [88]

    def test_a_genuinely_dead_judge_still_raises(self, cli):
        """The converse, so the test above cannot pass by `_is_transport_error`
        having been loosened into uselessness."""

        def fake(system, content, max_tokens=900, tier="premium", usage=None):
            if max_tokens == 300:
                return {"narrative": "n"}
            raise RuntimeError("You've hit your session limit")

        with pytest.raises(judge.JudgeUnavailable) as caught:
            _judge([_rule("tonal_neutrality")], fake)
        assert "tonal_neutrality" in str(caught.value)

    def test_the_output_cap_is_not_a_transport_error(self):
        assert not judge._is_transport_error(RuntimeError(OUTPUT_CAP_ERROR))
        assert judge._is_transport_error(RuntimeError("api_error_status 429"))
