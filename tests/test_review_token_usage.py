"""Unit tests for review LLM token-usage + cost accounting.

The review poller now tallies every Bedrock invocation's token usage into a
shared UsageAccumulator and reports the total tokens + USD cost on the result.
"""

import io
import json

from review import bedrock_judge
from review.bedrock_judge import UsageAccumulator


def test_accumulator_sums_tokens_and_costs(settings):
    settings.BEDROCK_INPUT_PRICE_PER_MTOK = 15.0
    settings.BEDROCK_OUTPUT_PRICE_PER_MTOK = 75.0

    usage = UsageAccumulator()
    usage.add(1_000_000, 200_000)
    usage.add(500_000, 0)

    out = usage.as_dict()
    assert out["calls"] == 2
    assert out["input_tokens"] == 1_500_000
    assert out["output_tokens"] == 200_000
    assert out["total_tokens"] == 1_700_000
    # 1.5M in @ $15/Mtok = $22.50 ; 0.2M out @ $75/Mtok = $15.00
    assert out["input_cost_usd"] == 22.5
    assert out["output_cost_usd"] == 15.0
    assert out["total_cost_usd"] == 37.5
    # The reported total must be exactly the sum of the reported parts.
    assert out["total_cost_usd"] == out["input_cost_usd"] + out["output_cost_usd"]
    assert out["model_id"] == settings.BEDROCK_MODEL_ID


def test_accumulator_tolerates_missing_token_counts():
    usage = UsageAccumulator()
    usage.add(None, None)
    out = usage.as_dict()
    assert out["calls"] == 1
    assert out["total_tokens"] == 0
    assert out["total_cost_usd"] == 0.0


def _fake_bedrock(input_tokens, output_tokens, text='{"score": 90}'):
    class _FakeClient:
        def invoke_model(self, modelId, body):
            payload = {
                "content": [{"text": text}],
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                },
            }
            return {"body": io.BytesIO(json.dumps(payload).encode())}

    return _FakeClient()


def test_invoke_text_records_usage(monkeypatch):
    monkeypatch.setattr(bedrock_judge, "_bedrock", lambda: _fake_bedrock(120, 34))
    usage = UsageAccumulator()
    parsed = bedrock_judge._invoke_once("grade this", usage=usage)
    assert parsed == {"score": 90}
    assert usage.input_tokens == 120
    assert usage.output_tokens == 34
    assert usage.calls == 1


def test_invoke_text_without_accumulator_is_optional(monkeypatch):
    monkeypatch.setattr(bedrock_judge, "_bedrock", lambda: _fake_bedrock(1, 1))
    # No usage accumulator passed: must not raise.
    assert bedrock_judge._invoke_once("grade this") == {"score": 90}
