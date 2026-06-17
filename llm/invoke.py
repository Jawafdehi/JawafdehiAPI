"""Generic LLM invocation entry points."""

import json

from llm import routing


def invoke_text(system, content, max_tokens, tier="premium", usage=None) -> str:
    """Invoke the LLM and return raw text response.

    Routes through the active provider for the given tier.

    Args:
        system: System prompt text
        content: User message content (string or list of blocks)
        max_tokens: Max tokens in response
        tier: "premium" or "cheap" (default "premium")
        usage: Optional UsageAccumulator to record token counts

    Returns:
        Raw text response (code fences stripped)
    """
    provider = routing.provider_for_tier(tier)
    model = provider.model_for_tier(tier)
    return provider.invoke_text(system, content, max_tokens, model, tier, usage)


def invoke_with_tools(
    system,
    content,
    tools,
    max_tokens=4000,
    tier="premium",
    usage=None,
    max_iterations=8,
) -> str:
    """Run an agentic tool-use loop, returning the model's final text.

    Supported on the API providers (bedrock, proxy); CLI-harness providers raise
    NotImplementedError. `tools` is a list of llm.tools.Tool.

    Args:
        system: System prompt text
        content: User message (a plain string)
        tools: list of llm.tools.Tool the model may call
        max_tokens: Max tokens per model turn (default 4000)
        tier: "premium" or "cheap" (default "premium")
        usage: Optional UsageAccumulator (accumulated across every loop turn)
        max_iterations: cap on model<->tool round-trips (default 8)

    Returns:
        The model's final assistant text (code fences stripped)
    """
    provider = routing.provider_for_tier(tier)
    model = provider.model_for_tier(tier)
    return provider.invoke_with_tools(
        system, content, max_tokens, model, tier, tools, usage, max_iterations
    )


def salvage_json(text):
    """Best-effort parse of a possibly-truncated/dirty JSON object.

    The LLM occasionally returns JSON that is cut off (max_tokens) or carries
    unescaped control chars from quoted source text. Try strict json first, then
    progressively: strip control chars, then close any unterminated string and
    balance braces/brackets so we recover whatever fields completed.

    Args:
        text: JSON text (possibly truncated or dirty)

    Returns:
        Parsed dict or list

    Raises:
        json.JSONDecodeError: If salvage fails completely
    """
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        pass
    # Drop raw control characters that break JSON string literals.
    cleaned = "".join(ch for ch in text if ch >= " " or ch in "\t")
    try:
        return json.loads(cleaned)
    except Exception:  # noqa: BLE001
        pass
    # Repair truncation: close an open string, then balance brackets.
    s = cleaned
    if s.count('"') % 2 == 1:
        s += '"'
    opens = s.count("{") - s.count("}")
    obrk = s.count("[") - s.count("]")
    s += "]" * max(0, obrk) + "}" * max(0, opens)
    return json.loads(s)  # may still raise; caller catches


def invoke_json(system, content, max_tokens=900, tier="premium", usage=None) -> dict:
    """Invoke the LLM once and parse its JSON, salvaging dirty/truncated output.

    The LLM occasionally prefixes the JSON with prose or gets cut off at
    max_tokens. We fetch the assistant text a single time and recover locally
    rather than re-invoking the model, which would double the call's cost/latency.

    Args:
        system: System prompt text
        content: User message content (string or list of blocks)
        max_tokens: Max tokens in response (default 900)
        tier: "premium" or "cheap" (default "premium")
        usage: Optional UsageAccumulator to record token counts

    Returns:
        Parsed JSON as dict or list

    Raises:
        json.JSONDecodeError: If salvage fails completely
    """
    text = invoke_text(system, content, max_tokens, tier, usage)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return salvage_json(text)
