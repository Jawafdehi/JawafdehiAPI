"""Unit tests for shared enrichment utilities (_enrich_utils).

Focus on the date-conversion tool and the tool-use LLM loop added for the
API-driven timeline enrichment command. These tests do not touch the database.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from django.core.management.base import CommandError

from cases.management.commands._enrich_utils import (
    CONVERT_DATE_TOOL,
    CONVERT_DATE_TOOL_ANTHROPIC,
    call_bedrock_with_tools,
    call_llm_with_tools,
    convert_date,
)

# ── convert_date ─────────────────────────────────────────────────────────────


class TestConvertDate:
    def test_bs_to_ad(self):
        result = convert_date(["2081-10-27"], "bs_to_ad")
        assert result == {"2081-10-27": "2025-02-09"}

    def test_ad_to_bs(self):
        result = convert_date(["2025-02-09"], "ad_to_bs")
        assert result == {"2025-02-09": "2081-10-27"}

    def test_batch_conversion(self):
        result = convert_date(["2046-03-30", "2077-03-31"], "bs_to_ad")
        assert result["2046-03-30"] == "1989-07-14"
        assert result["2077-03-31"] == "2020-07-15"

    def test_normalizes_slashes_and_devanagari_digits(self):
        result = convert_date(["२०८१/१०/२७"], "bs_to_ad")
        assert result["२०८१/१०/२७"] == "2025-02-09"

    def test_malformed_date_reports_error_per_entry(self):
        result = convert_date(["not-a-date", "2081-10-27"], "bs_to_ad")
        assert result["not-a-date"].startswith("Error")
        assert result["2081-10-27"] == "2025-02-09"

    def test_out_of_range_date_reports_error_not_crash(self):
        # nepali raises its own exception types (not ValueError/TypeError) for
        # out-of-range dates; convert_date must report per-entry, not crash.
        result = convert_date(["9999-13-40", "2081-10-27"], "bs_to_ad")
        assert result["9999-13-40"].startswith("Error")
        assert result["2081-10-27"] == "2025-02-09"

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError):
            convert_date(["2081-10-27"], "sideways")

    def test_non_list_raises(self):
        with pytest.raises(ValueError):
            convert_date("2081-10-27", "bs_to_ad")


# ── call_llm_with_tools ──────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeSession:
    """Returns a queued sequence of chat-completion responses, one per POST."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.requests.append(json)
        return _FakeResponse(self._responses.pop(0))


def _assistant_text(text):
    return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}


def _assistant_tool_call(call_id, name, arguments):
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }


def test_returns_text_when_no_tool_calls():
    session = _FakeSession([_assistant_text("final answer")])
    out = call_llm_with_tools(
        system_prompt="sys",
        user_prompt="usr",
        model="m",
        base_url="http://x/v1",
        api_key="k",
        session=session,
        tools=[CONVERT_DATE_TOOL],
        tool_executors={"convert_date": convert_date},
    )
    assert out == "final answer"
    assert len(session.requests) == 1


def test_executes_tool_call_then_returns_final_text():
    session = _FakeSession(
        [
            _assistant_tool_call(
                "c1",
                "convert_date",
                {
                    "dates": ["2081-10-27"],
                    "mode": "bs_to_ad",
                },
            ),
            _assistant_text('[{"date": "2025-02-09", "title": "x"}]'),
        ]
    )
    out = call_llm_with_tools(
        system_prompt="sys",
        user_prompt="usr",
        model="m",
        base_url="http://x/v1",
        api_key="k",
        session=session,
        tools=[CONVERT_DATE_TOOL],
        tool_executors={"convert_date": convert_date},
    )
    assert "2025-02-09" in out
    # Two round-trips: tool call, then final answer.
    assert len(session.requests) == 2
    # The second request must include the tool result message.
    second_msgs = session.requests[1]["messages"]
    tool_msgs = [m for m in second_msgs if m.get("role") == "tool"]
    assert tool_msgs and "2025-02-09" in tool_msgs[0]["content"]


def test_unknown_tool_is_reported_to_model_not_fatal():
    session = _FakeSession(
        [
            _assistant_tool_call("c1", "no_such_tool", {}),
            _assistant_text("recovered"),
        ]
    )
    out = call_llm_with_tools(
        system_prompt="sys",
        user_prompt="usr",
        model="m",
        base_url="http://x/v1",
        api_key="k",
        session=session,
        tools=[CONVERT_DATE_TOOL],
        tool_executors={"convert_date": convert_date},
    )
    assert out == "recovered"
    tool_msg = [m for m in session.requests[1]["messages"] if m.get("role") == "tool"][
        0
    ]
    assert "unknown tool" in tool_msg["content"]


def test_tool_executor_exception_is_reported_not_fatal():
    """A tool executor that raises any exception is reported to the model."""

    def _boom(**kwargs):
        raise RuntimeError("executor blew up")

    session = _FakeSession(
        [
            _assistant_tool_call("c1", "boom", {"x": 1}),
            _assistant_text("recovered after tool error"),
        ]
    )
    out = call_llm_with_tools(
        system_prompt="sys",
        user_prompt="usr",
        model="m",
        base_url="http://x/v1",
        api_key="k",
        session=session,
        tools=[CONVERT_DATE_TOOL],
        tool_executors={"boom": _boom},
    )
    assert out == "recovered after tool error"
    tool_msg = [m for m in session.requests[1]["messages"] if m.get("role") == "tool"][
        0
    ]
    assert "executor blew up" in tool_msg["content"]


def test_exceeding_tool_round_budget_raises():
    # Always returns a tool call, never a final answer.
    responses = [
        _assistant_tool_call(
            f"c{i}", "convert_date", {"dates": ["2081-10-27"], "mode": "bs_to_ad"}
        )
        for i in range(5)
    ]
    session = _FakeSession(responses)
    with pytest.raises(CommandError, match="tool-use rounds"):
        call_llm_with_tools(
            system_prompt="sys",
            user_prompt="usr",
            model="m",
            base_url="http://x/v1",
            api_key="k",
            session=session,
            tools=[CONVERT_DATE_TOOL],
            tool_executors={"convert_date": convert_date},
            max_tool_rounds=3,
        )


# ── call_bedrock_with_tools ──────────────────────────────────────────────────


def _bedrock_body(payload):
    """Wrap a payload dict as a Bedrock invoke_model response (body has .read())."""
    resp_body = MagicMock()
    resp_body.read.return_value = json.dumps(payload).encode("utf-8")
    return {"body": resp_body}


def _fake_bedrock_client(payloads):
    """A bedrock-runtime client whose invoke_model returns queued payloads."""
    client = MagicMock()
    client.invoke_model.side_effect = [_bedrock_body(p) for p in payloads]
    return client


def _patch_boto3(client):
    """Patch boto3.Session(...).client(...) to return the given fake client."""
    session = MagicMock()
    session.client.return_value = client
    return patch("boto3.Session", return_value=session)


def test_bedrock_returns_text_when_no_tool_use():
    client = _fake_bedrock_client(
        [{"stop_reason": "end_turn", "content": [{"type": "text", "text": "final"}]}]
    )
    with _patch_boto3(client):
        out = call_bedrock_with_tools(
            system_prompt="sys",
            user_prompt="usr",
            model_id="global.anthropic.claude-opus-4-8",
            tools=[CONVERT_DATE_TOOL_ANTHROPIC],
            tool_executors={"convert_date": convert_date},
        )
    assert out == "final"
    assert client.invoke_model.call_count == 1


def test_bedrock_executes_tool_use_then_returns_text():
    client = _fake_bedrock_client(
        [
            {
                "stop_reason": "tool_use",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tu1",
                        "name": "convert_date",
                        "input": {"dates": ["2081-10-27"], "mode": "bs_to_ad"},
                    }
                ],
            },
            {
                "stop_reason": "end_turn",
                "content": [{"type": "text", "text": "done 2025-02-09"}],
            },
        ]
    )
    with _patch_boto3(client):
        out = call_bedrock_with_tools(
            system_prompt="sys",
            user_prompt="usr",
            model_id="global.anthropic.claude-opus-4-8",
            tools=[CONVERT_DATE_TOOL_ANTHROPIC],
            tool_executors={"convert_date": convert_date},
        )
    assert "2025-02-09" in out
    assert client.invoke_model.call_count == 2
    # 2nd call must carry a tool_result block with the conversion output.
    second_body = json.loads(client.invoke_model.call_args_list[1].kwargs["body"])
    user_turns = [m for m in second_body["messages"] if m["role"] == "user"]
    tool_result = user_turns[-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "tu1"
    assert "2025-02-09" in tool_result["content"]


def test_bedrock_unknown_tool_reported_not_fatal():
    client = _fake_bedrock_client(
        [
            {
                "stop_reason": "tool_use",
                "content": [
                    {"type": "tool_use", "id": "t", "name": "nope", "input": {}}
                ],
            },
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": "ok"}]},
        ]
    )
    with _patch_boto3(client):
        out = call_bedrock_with_tools(
            system_prompt="sys",
            user_prompt="usr",
            model_id="m",
            tools=[CONVERT_DATE_TOOL_ANTHROPIC],
            tool_executors={"convert_date": convert_date},
        )
    assert out == "ok"
    second_body = json.loads(client.invoke_model.call_args_list[1].kwargs["body"])
    tr = [m for m in second_body["messages"] if m["role"] == "user"][-1]["content"][0]
    assert tr.get("is_error") and "unknown tool" in tr["content"]


def test_bedrock_exceeding_tool_rounds_raises():
    payloads = [
        {
            "stop_reason": "tool_use",
            "content": [
                {
                    "type": "tool_use",
                    "id": f"t{i}",
                    "name": "convert_date",
                    "input": {"dates": ["2081-10-27"], "mode": "bs_to_ad"},
                }
            ],
        }
        for i in range(5)
    ]
    client = _fake_bedrock_client(payloads)
    with _patch_boto3(client):
        with pytest.raises(CommandError, match="tool-use rounds"):
            call_bedrock_with_tools(
                system_prompt="sys",
                user_prompt="usr",
                model_id="m",
                tools=[CONVERT_DATE_TOOL_ANTHROPIC],
                tool_executors={"convert_date": convert_date},
                max_tool_rounds=3,
            )
