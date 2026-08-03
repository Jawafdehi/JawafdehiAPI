"""Tests for the adequacy judge (casework/common/judge.py).

The judge decides whether a field value is real or a template stub. Two
properties carry all the weight:

- It FAILS TOWARD REGENERATING. Every failure mode -- a dead provider, an
  unparseable verdict, a truncated reply -- must return `adequate=False`. The
  opposite default silently keeps template text on the public case list.
- It spends no LLM call on text that cannot possibly be adequate.
"""
import json

import pytest

from casework.common.judge import (
    _JUDGE_MIN_CHARS,
    _parse_judge_verdict,
    judge_description_adequacy,
)

REAL_TEASER = "काठमाडौं महानगरपालिकाको ठेक्कामा रु ३३ करोड हिनामिना भएको आरोप।"


def _stub(payload, calls=None):
    def stub(**kw):
        if calls is not None:
            calls.append(kw)
        return payload if isinstance(payload, str) else json.dumps(payload)
    return stub


class TestNoCallShortCircuit:
    def test_blank_text_is_inadequate_without_an_llm_call(self):
        calls = []
        adequate, reason = judge_description_adequacy(
            "", kind="teaser", invoke_text=_stub({"adequate": True}, calls))
        assert adequate is False
        assert "blank or too short" in reason
        assert calls == []

    def test_none_is_inadequate_without_a_call(self):
        calls = []
        adequate, _ = judge_description_adequacy(
            None, kind="teaser", invoke_text=_stub({"adequate": True}, calls))
        assert adequate is False
        assert calls == []

    def test_text_under_the_floor_is_inadequate_without_a_call(self):
        calls = []
        adequate, _ = judge_description_adequacy(
            "क" * (_JUDGE_MIN_CHARS - 1), kind="teaser",
            invoke_text=_stub({"adequate": True}, calls))
        assert adequate is False
        assert calls == []

    def test_text_at_the_floor_does_reach_the_model(self):
        calls = []
        judge_description_adequacy(
            "क" * _JUDGE_MIN_CHARS, kind="teaser",
            invoke_text=_stub({"adequate": True, "reason": "ok"}, calls))
        assert len(calls) == 1


class TestVerdictHandling:
    def test_an_adequate_verdict_is_returned_with_its_reason(self):
        adequate, reason = judge_description_adequacy(
            REAL_TEASER, kind="teaser",
            invoke_text=_stub({"adequate": True, "reason": "names an amount"}))
        assert adequate is True
        assert reason == "names an amount"

    def test_an_inadequate_verdict_is_returned(self):
        adequate, reason = judge_description_adequacy(
            REAL_TEASER, kind="teaser",
            invoke_text=_stub({"adequate": False, "reason": "boilerplate"}))
        assert adequate is False
        assert reason == "boilerplate"

    def test_a_missing_reason_gets_a_placeholder_not_a_blank(self):
        _, reason = judge_description_adequacy(
            REAL_TEASER, kind="teaser", invoke_text=_stub({"adequate": True}))
        assert reason == "(no reason given)"

    def test_the_prompt_carries_the_field_kind_and_the_context(self):
        calls = []
        judge_description_adequacy(
            REAL_TEASER, kind="case card short description",
            invoke_text=_stub({"adequate": True}, calls),
            context="title=काठमाडौं ठेक्का")
        content = calls[0]["content"]
        assert "case card short description" in content
        assert "काठमाडौं ठेक्का" in content
        assert REAL_TEASER in content

    def test_the_context_line_is_omitted_when_empty(self):
        calls = []
        judge_description_adequacy(
            REAL_TEASER, kind="teaser", invoke_text=_stub({"adequate": True}, calls))
        assert "CONTEXT:" not in calls[0]["content"]

    def test_the_cheap_tier_is_the_default(self):
        calls = []
        judge_description_adequacy(
            REAL_TEASER, kind="teaser", invoke_text=_stub({"adequate": True}, calls))
        assert calls[0]["tier"] == "cheap"
        assert calls[0]["max_tokens"] == 300

    def test_the_tier_is_overridable(self):
        calls = []
        judge_description_adequacy(
            REAL_TEASER, kind="teaser",
            invoke_text=_stub({"adequate": True}, calls), tier="premium")
        assert calls[0]["tier"] == "premium"


class TestFailsTowardRegenerating:
    """Every failure mode returns adequate=False.

    A needless regeneration costs one cheap call. Silently keeping a placeholder
    ships template text to the public case list, where nothing distinguishes it
    from real editorial output.
    """

    def test_a_failed_provider_call_is_inadequate(self):
        def boom(**kw):
            raise RuntimeError("provider down")

        adequate, reason = judge_description_adequacy(
            REAL_TEASER, kind="teaser", invoke_text=boom)
        assert adequate is False
        assert "judge call failed" in reason
        assert "provider down" in reason

    def test_an_unparseable_reply_is_inadequate(self):
        adequate, reason = judge_description_adequacy(
            REAL_TEASER, kind="teaser", invoke_text=_stub("I think it's fine!"))
        assert adequate is False
        assert "unparseable" in reason

    def test_an_object_without_the_adequate_key_is_inadequate(self):
        adequate, _ = judge_description_adequacy(
            REAL_TEASER, kind="teaser", invoke_text=_stub({"verdict": "good"}))
        assert adequate is False

    def test_a_non_boolean_adequate_is_inadequate(self):
        """`{"adequate": "yes"}` is not a verdict. Truthiness would read it as
        adequate and keep a stub."""
        adequate, _ = judge_description_adequacy(
            REAL_TEASER, kind="teaser", invoke_text=_stub({"adequate": "yes"}))
        assert adequate is False


class TestParseJudgeVerdict:
    def test_parses_a_plain_object(self):
        assert _parse_judge_verdict(
            '{"adequate": false, "reason": "stub"}') == (False, "stub")

    def test_parses_a_fenced_object(self):
        body = '```json\n{"adequate": true, "reason": "ok"}\n```'
        assert _parse_judge_verdict(body) == (True, "ok")

    def test_scans_past_a_near_miss_object(self):
        """The `predicate` is what keeps the scan going.

        A reply whose first object has a non-boolean `adequate` must not end the
        search -- the real verdict may be the next one.
        """
        body = '{"adequate": "maybe"} then {"adequate": true, "reason": "ok"}'
        assert _parse_judge_verdict(body) == (True, "ok")

    def test_returns_none_when_no_boolean_verdict_exists(self):
        assert _parse_judge_verdict('{"adequate": "yes"}') is None
        assert _parse_judge_verdict("not json") is None
        assert _parse_judge_verdict("") is None

    @pytest.mark.parametrize("reason", [None, 42, "   "])
    def test_a_junk_reason_becomes_the_placeholder(self, reason):
        body = json.dumps({"adequate": True, "reason": reason})
        assert _parse_judge_verdict(body) == (True, "(no reason given)")


def test_the_system_prompt_names_placeholder_shapes_in_both_scripts():
    """The judge has to recognise Nepali stubs, not only English ones -- the
    2,666 production stubs are Devanagari."""
    from casework.common.judge import _JUDGE_SYSTEM_PROMPT

    assert "खाली" in _JUDGE_SYSTEM_PROMPT
    assert "TODO" in _JUDGE_SYSTEM_PROMPT
    assert "fits any case" in _JUDGE_SYSTEM_PROMPT
