"""Unit tests for review LLM token-usage metrics.

The review poller tallies every LLM invocation's token usage into a shared
UsageAccumulator and reports the totals on the result.
"""

from llm.usage import UsageAccumulator


def test_accumulator_sums_tokens(settings):
    usage = UsageAccumulator()
    usage.add(1_000_000, 200_000)
    usage.add(500_000, 0)

    out = usage.as_dict()
    assert out["calls"] == 2
    assert out["input_tokens"] == 1_500_000
    assert out["output_tokens"] == 200_000
    assert out["total_tokens"] == 1_700_000
    assert out["model_id"] == settings.BEDROCK_MODEL_ID


def test_accumulator_tolerates_missing_token_counts():
    usage = UsageAccumulator()
    usage.add(None, None)
    out = usage.as_dict()
    assert out["calls"] == 1
    assert out["total_tokens"] == 0
