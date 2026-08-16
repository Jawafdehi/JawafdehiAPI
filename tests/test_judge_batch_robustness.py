"""Batched CLI grading must not silently neutral-score omitted rules.

A batch reply covers up to RULE_BATCH_SIZE rules in one JSON object; the model
can omit keys (or truncation can drop them after salvage). Omitted rules are
re-graded per-rule, and a rule that STILL has no grade must not fail a gate —
a placeholder 50 is not a verdict.
"""

from unittest import mock

from review import judge, scorer
from review.code_rules import CodeRule


def _rule(key, *, is_gate=False, gate_min=50):
    return {
        "key": key,
        "title": key,
        "description": "d",
        "good_examples": "",
        "bad_examples": "",
        "is_gate": is_gate,
    }


def _cli_settings(settings):
    settings.REVIEW_LLM_PROVIDER_PREMIUM = "claude_cli"
    settings.REVIEW_LLM_PROVIDER_CHEAP = "claude_cli"


def test_batch_omitted_rule_is_regraded_per_rule(settings):
    _cli_settings(settings)
    per_rule_calls = []

    def fake_invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        if max_tokens == 300:  # narrative task
            return {"narrative": "n"}
        text = content if isinstance(content, str) else content[-1]["text"]
        if "EACH of the" in text:  # batch task — omit the gate rule entirely
            rules = {}
            if "routine" in text:
                rules["routine"] = {"score": 70, "rationale": "ok"}
            return {"rules": rules}
        per_rule_calls.append(text)
        return {"score": 88, "rationale": "regraded in isolation"}

    with mock.patch.object(judge, "invoke_json", side_effect=fake_invoke_json):
        judged = judge.judge_rules(
            "case", "excerpts", "label",
            [_rule("hard_gate", is_gate=True), _rule("routine")],
            n_samples=1,
        )

    # The omitted gate rule was re-graded per-rule and carries the real score.
    assert len(per_rule_calls) == 1 and "hard_gate" in per_rule_calls[0]
    assert judged["hard_gate"]["samples"] == [88]
    assert judged["routine"]["samples"] == [70]


def test_still_ungraded_rule_has_no_fabricated_sample(settings):
    """The model ANSWERED but returned no usable score — a content problem.

    Every call lands, so there is nothing unavailable; the rule just ends up
    ungraded. That stays non-fatal, and must not fabricate a sample.
    """
    _cli_settings(settings)

    def fake_invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        if max_tokens == 300:
            return {"narrative": "n"}
        text = content if isinstance(content, str) else content[-1]["text"]
        if "EACH of the" in text:
            return {"rules": {}}  # batch answers nothing
        return {"rationale": "cannot assess this one"}  # replies, but no score

    with mock.patch.object(judge, "invoke_json", side_effect=fake_invoke_json):
        judged = judge.judge_rules(
            "case", "excerpts", "label", [_rule("hard_gate", is_gate=True)],
            n_samples=1,
        )

    # Neutral display mean, but NO fabricated sample — the scorer can tell
    # "the judge said 50" apart from "the judge never answered".
    assert judged["hard_gate"]["samples"] == []
    assert judged["hard_gate"]["mean"] == 50.0


def test_unreachable_judge_raises_instead_of_neutral_scoring(settings):
    """A rule left ungraded by a FAILED CALL is unavailability, not a low score."""
    _cli_settings(settings)

    def fake_invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        if max_tokens == 300:
            return {"narrative": "n"}
        text = content if isinstance(content, str) else content[-1]["text"]
        if "EACH of the" in text:
            return {"rules": {}}
        raise RuntimeError("claude_cli failed (rc=1): api_error_status 429")

    with mock.patch.object(judge, "invoke_json", side_effect=fake_invoke_json):
        try:
            judge.judge_rules(
                "case", "excerpts", "label", [_rule("hard_gate", is_gate=True)],
                n_samples=1,
            )
        except judge.JudgeUnavailable as e:
            assert "429" in str(e) and "hard_gate" in str(e)
        else:
            raise AssertionError("expected JudgeUnavailable")


def test_quota_death_partway_through_fails_the_whole_review(settings):
    """The real 2026-08-12 shape: early rules grade, then the session cap hits.

    The surviving grades made this look like a finished review scoring in the
    low 70s. It is an unfinished one, and must not be scored at all.
    """
    _cli_settings(settings)

    def fake_invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        if max_tokens == 300:
            return {"narrative": "n"}
        text = content if isinstance(content, str) else content[-1]["text"]
        if "early" in text:  # this one got in before the cap
            return {"score": 85, "rationale": "graded fine"}
        raise RuntimeError("You've hit your session limit")

    # batch_size 1 => one call per rule, so one grades and the rest die.
    settings.REVIEW_RULE_BATCH_SIZE = 1
    with mock.patch.object(judge, "invoke_json", side_effect=fake_invoke_json):
        try:
            judge.judge_rules(
                "case", "excerpts", "label",
                [_rule("early"), _rule("late_one"), _rule("late_two")],
                n_samples=1,
            )
        except judge.JudgeUnavailable as e:
            # Only the rules whose own call died are named — `early` graded.
            assert "session limit" in str(e)
            assert "2/3" in str(e)
            assert "late_one" in str(e) and "late_two" in str(e)
            assert "early" not in str(e)
        else:
            raise AssertionError("expected JudgeUnavailable on partial failure")


def test_dead_narrative_call_does_not_condemn_an_ungraded_rule(settings):
    """A failed narrative call is not evidence about any rule's reachability.

    The narrative is one cheap call alongside the grading ones. When it dies and
    a rule separately comes back scoreless, the rule was still reached — the two
    facts are unrelated, and treating them as one failure would dead-letter a
    review over a cosmetic call.
    """
    _cli_settings(settings)
    settings.REVIEW_RULE_BATCH_SIZE = 1

    def fake_invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        if max_tokens == 300:  # narrative task
            raise RuntimeError("claude_cli failed (rc=1): api_error_status 429")
        text = content if isinstance(content, str) else content[-1]["text"]
        if "graded" in text:
            return {"score": 77, "rationale": "fine"}
        return {"rationale": "cannot assess this one"}  # replies, but no score

    with mock.patch.object(judge, "invoke_json", side_effect=fake_invoke_json):
        judged = judge.judge_rules(
            "case", "excerpts", "label", [_rule("graded"), _rule("scoreless")],
            n_samples=1,
        )

    assert judged["graded"]["samples"] == [77]
    assert judged["scoreless"]["samples"] == [] and judged["scoreless"]["mean"] == 50.0


def test_unexpected_bug_in_a_rule_call_is_not_unavailability(settings):
    """A TypeError is our bug, not a dead credential — keep degrading leniently.

    It leaves the rule ungraded exactly as a 429 would, so classifying by "was
    there an error" alone would fail the job over a code defect. Only a call that
    never landed is fatal.
    """
    _cli_settings(settings)
    settings.REVIEW_RULE_BATCH_SIZE = 1

    def fake_invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        if max_tokens == 300:
            return {"narrative": "n"}
        raise TypeError("bug in prompt building")

    with mock.patch.object(judge, "invoke_json", side_effect=fake_invoke_json):
        judged = judge.judge_rules(
            "case", "excerpts", "label", [_rule("hard_gate", is_gate=True)],
            n_samples=1,
        )

    assert judged["hard_gate"]["samples"] == [] and judged["hard_gate"]["mean"] == 50.0


def test_total_content_failure_is_not_reported_as_unavailability(settings):
    """Every call landed and every one was useless — that is not unreachable.

    It still raises, so the scorer records `judge_error` over neutral scores, but
    as a plain RuntimeError the scorer catches rather than a job-killing one.
    """
    _cli_settings(settings)
    settings.REVIEW_RULE_BATCH_SIZE = 1

    def fake_invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        return {}  # answers everything, says nothing

    with mock.patch.object(judge, "invoke_json", side_effect=fake_invoke_json):
        try:
            judge.judge_rules(
                "case", "excerpts", "label", [_rule("hard_gate", is_gate=True)],
                n_samples=1,
            )
        except judge.JudgeUnavailable:
            raise AssertionError("content failure misreported as unavailability")
        except RuntimeError as e:
            assert "judge calls failed" in str(e)
        else:
            raise AssertionError("expected RuntimeError")


def test_scorer_propagates_unavailable_instead_of_scoring():
    """score_case must not turn unavailability into a ~70 with low confidence."""
    gate = CodeRule(0, {
        "key": "hard_gate", "title": "Hard gate", "kind": CodeRule.KIND_LLM,
        "is_gate": True, "gate_min": 60, "weight": 1.0,
    })

    class _Cfg:
        pass_threshold, revise_threshold, llm_samples = 80, 40, 1

    boom = judge.JudgeUnavailable("All 3 judge calls failed: 429 session limit")
    with mock.patch.object(scorer.judge, "judge_rules", side_effect=boom):
        try:
            scorer.score_case({"title": "t"}, [], [gate], _Cfg(), source_analyses=[])
        except judge.JudgeUnavailable:
            pass
        else:
            raise AssertionError("score_case swallowed JudgeUnavailable")


def test_scorer_still_degrades_on_an_unexpected_judge_bug():
    """Non-transport judge bugs keep the old lenient behaviour, not a hard fail."""
    gate = CodeRule(0, {
        "key": "hard_gate", "title": "Hard gate", "kind": CodeRule.KIND_LLM,
        "is_gate": True, "gate_min": 60, "weight": 1.0,
    })

    class _Cfg:
        pass_threshold, revise_threshold, llm_samples = 80, 40, 1

    with mock.patch.object(
        scorer.judge, "judge_rules", side_effect=TypeError("bug in prompt building")
    ):
        result = scorer.score_case({"title": "t"}, [], [gate], _Cfg(), source_analyses=[])
    assert result["judge_error"] and "bug in prompt building" in result["judge_error"]


def test_ungraded_gate_rule_does_not_reject_the_case():
    gate = CodeRule(0, {
        "key": "hard_gate", "title": "Hard gate", "kind": CodeRule.KIND_LLM,
        "is_gate": True, "gate_min": 60, "weight": 1.0,
    })

    class _Cfg:
        pass_threshold, revise_threshold, llm_samples = 80, 40, 1

    judged = {
        "_narrative": "n", "_n_samples": 1,
        "hard_gate": {
            "mean": 50.0, "variance": 0.0, "std": 0.0, "samples": [],
            "rationale": "", "issues": [], "suggestions": [],
        },
    }
    with mock.patch.object(scorer.judge, "judge_rules", return_value=judged):
        result = scorer.score_case({"title": "t"}, [], [gate], _Cfg(), source_analyses=[])

    rr = result["rules"][0]
    assert rr["samples"] == [] and rr["gate_failed"] is False
    assert result["disposition"] != "REJECT"
    assert "no grade" in result["narrative"] or "neutral defaults" in result["narrative"]

    # A REAL below-minimum grade still fails the gate.
    judged["hard_gate"]["samples"] = [50]
    with mock.patch.object(scorer.judge, "judge_rules", return_value=judged):
        result = scorer.score_case({"title": "t"}, [], [gate], _Cfg(), source_analyses=[])
    assert result["rules"][0]["gate_failed"] is True
    assert result["disposition"] == "REJECT"
