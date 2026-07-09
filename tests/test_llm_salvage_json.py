"""Regression tests for ``llm.invoke.salvage_json``.

``salvage_json`` is best-effort string surgery for dirty / truncated LLM JSON.
Its documented contract (see the docstring in ``llm/invoke.py``):

* returns a parsed ``dict`` or ``list`` when it can recover something, and
* raises ``json.JSONDecodeError`` when salvage fails completely.

The repair pipeline is three staged attempts:

1. strict ``json.loads``;
2. strip raw control chars (anything below 0x20 except tab) and re-parse;
3. close one unterminated string (odd ``"`` count) and balance ``[]``/``{}``,
   then a final ``json.loads`` that *may* still raise.

These tests characterize the CURRENT behavior branch by branch. They do not
require the DB or Django models — ``salvage_json`` is pure.
"""

import json

import pytest

from llm.invoke import salvage_json


# ---------------------------------------------------------------------------
# Stage 1: strict parse passthrough.
# ---------------------------------------------------------------------------
def test_clean_object_passthrough():
    assert salvage_json('{"a": 1, "b": "two"}') == {"a": 1, "b": "two"}


def test_clean_list_passthrough():
    assert salvage_json("[1, 2, 3]") == [1, 2, 3]


def test_clean_nested_passthrough():
    text = '{"outer": {"inner": [1, {"k": "v"}]}}'
    assert salvage_json(text) == {"outer": {"inner": [1, {"k": "v"}]}}


# ---------------------------------------------------------------------------
# Stage 2: control-char stripping.
# ---------------------------------------------------------------------------
def test_embedded_nul_control_char_stripped():
    # A raw NUL inside a string literal breaks strict JSON; stage 2 drops it.
    result = salvage_json('{"a": "line1\x00line2"}')
    assert result == {"a": "line1line2"}


def test_embedded_raw_newline_in_string_stripped():
    # A literal newline inside a quoted string is invalid JSON; it is stripped
    # rather than escaped, so the two halves are concatenated.
    result = salvage_json('{"a": "line1\nline2"}')
    assert result == {"a": "line1line2"}


def test_tab_is_preserved_as_whitespace_between_tokens():
    # Tab is explicitly allowed through the control-char filter. Here it sits
    # as insignificant whitespace between tokens, so strict parse already
    # succeeds and the value is unchanged.
    assert salvage_json('{"a":\t1}') == {"a": 1}


# ---------------------------------------------------------------------------
# Stage 3: truncation repair (unterminated string + bracket balancing).
# ---------------------------------------------------------------------------
def test_unterminated_string_closed():
    # Truncated mid-string: odd quote count -> append a closing quote, then
    # balance the open brace.
    assert salvage_json('{"a": "hello') == {"a": "hello"}


def test_unbalanced_brace_closed():
    assert salvage_json('{"a": {"b": 1}') == {"a": {"b": 1}}


def test_unbalanced_bracket_closed():
    assert salvage_json('{"a": [1, 2') == {"a": [1, 2]}


def test_truncated_object_with_open_string_and_brackets():
    # Combined: an open string and open bracket/brace all get repaired. Note the
    # brackets are closed *inside-out* (all "]" before all "}").
    assert salvage_json('{"items": ["one", "tw') == {"items": ["one", "tw"]}


# ---------------------------------------------------------------------------
# Failure contract: salvage that cannot recover raises JSONDecodeError.
# ---------------------------------------------------------------------------
def test_trailing_comma_is_not_repaired_and_raises():
    # Trailing commas are NOT part of the repair pipeline; strict json rejects
    # them and none of the stages fix them, so the final parse raises.
    with pytest.raises(json.JSONDecodeError):
        salvage_json('{"a": 1,}')


def test_json_fences_are_not_stripped_and_raise():
    # salvage_json does NOT strip ```json code fences (that stripping lives in
    # the provider layer's invoke_text). Given a fenced blob it cannot recover.
    fenced = "```json\n{\"a\": 1}\n```"
    with pytest.raises(json.JSONDecodeError):
        salvage_json(fenced)


def test_leading_prose_around_object_raises():
    # Prose preceding a JSON object is not stripped; salvage cannot recover it.
    with pytest.raises(json.JSONDecodeError):
        salvage_json('Here is the JSON you asked for: {"a": 1}')


def test_total_garbage_raises():
    with pytest.raises(json.JSONDecodeError):
        salvage_json("this is not json at all")


def test_empty_string_raises():
    with pytest.raises(json.JSONDecodeError):
        salvage_json("")


# ---------------------------------------------------------------------------
# invoke_json integration contract: salvage_json is the fallback path.
# ---------------------------------------------------------------------------
def test_invoke_json_recovers_truncated_output(monkeypatch):
    """``invoke_json`` parses clean output directly and falls back to
    ``salvage_json`` on a JSONDecodeError, without re-invoking the model."""
    from llm import invoke as invoke_mod

    calls = {"n": 0}

    def fake_invoke_text(system, content, max_tokens, tier, usage):
        calls["n"] += 1
        # Truncated: strict parse fails, salvage recovers the completed field.
        return '{"verdict": "guilty", "note": "the source sa'

    monkeypatch.setattr(invoke_mod, "invoke_text", fake_invoke_text)

    result = invoke_mod.invoke_json("sys", "prompt")
    assert result == {"verdict": "guilty", "note": "the source sa"}
    # Only one model call — recovery is local, not a re-invocation.
    assert calls["n"] == 1


def test_invoke_json_propagates_unrecoverable_error(monkeypatch):
    from llm import invoke as invoke_mod

    def fake_invoke_text(system, content, max_tokens, tier, usage):
        return "totally unparseable"

    monkeypatch.setattr(invoke_mod, "invoke_text", fake_invoke_text)

    with pytest.raises(json.JSONDecodeError):
        invoke_mod.invoke_json("sys", "prompt")
