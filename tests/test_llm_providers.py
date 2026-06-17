"""Transport tests for the generic llm providers (re-homed from the old
review.bedrock_judge tests after the transport moved into the llm package)."""

import io
import json

from llm.providers.bedrock import BedrockProvider
from llm.usage import UsageAccumulator


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


def test_bedrock_invoke_records_usage(monkeypatch):
    p = BedrockProvider()
    monkeypatch.setattr(p, "_client", lambda: _fake_bedrock(120, 34))
    usage = UsageAccumulator()
    text = p.invoke_text("sys", "grade this", 900, "model-x", "premium", usage)
    assert text == '{"score": 90}'
    assert usage.input_tokens == 120
    assert usage.output_tokens == 34
    assert usage.calls == 1
    bucket = usage.as_dict()["by_provider"][0]
    assert bucket["provider"] == "bedrock"
    assert bucket["tier"] == "premium"
    assert bucket["model"] == "model-x"


def test_bedrock_invoke_without_accumulator_is_optional(monkeypatch):
    p = BedrockProvider()
    monkeypatch.setattr(p, "_client", lambda: _fake_bedrock(1, 1))
    # No usage accumulator passed: must not raise.
    assert p.invoke_text("sys", "grade this", 900, "m", "cheap") == '{"score": 90}'
