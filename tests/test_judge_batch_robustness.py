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
    _cli_settings(settings)

    def fake_invoke_json(system, content, max_tokens=900, tier="premium", usage=None):
        if max_tokens == 300:
            return {"narrative": "n"}
        text = content if isinstance(content, str) else content[-1]["text"]
        if "EACH of the" in text:
            return {"rules": {}}  # batch answers nothing
        raise RuntimeError("per-rule retry also failed")

    with mock.patch.object(judge, "invoke_json", side_effect=fake_invoke_json):
        judged = judge.judge_rules(
            "case", "excerpts", "label", [_rule("hard_gate", is_gate=True)],
            n_samples=1,
        )

    # Neutral display mean, but NO fabricated sample — the scorer can tell
    # "the judge said 50" apart from "the judge never answered".
    assert judged["hard_gate"]["samples"] == []
    assert judged["hard_gate"]["mean"] == 50.0


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
