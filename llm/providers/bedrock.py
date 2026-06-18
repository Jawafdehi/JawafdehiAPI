"""AWS Bedrock LLM provider."""

import json

import boto3
from botocore.config import Config
from django.conf import settings

from llm.providers.base import Provider, strip_code_fence

_client = None


class BedrockProvider(Provider):
    """LLM provider using AWS Bedrock."""

    name = "bedrock"

    def _client(self):
        """Singleton boto3 Bedrock client."""
        global _client
        if _client is None:
            session = boto3.Session(
                profile_name=settings.AWS_PROFILE or None,
                region_name=settings.AWS_REGION,
            )
            max_workers = int(getattr(settings, "BEDROCK_MAX_WORKERS", 8))
            _client = session.client(
                "bedrock-runtime",
                config=Config(
                    read_timeout=120,
                    connect_timeout=15,
                    retries={"max_attempts": 4, "mode": "adaptive"},
                    max_pool_connections=max_workers + 4,
                ),
            )
        return _client

    def invoke_text(self, system, content, max_tokens, model_id, tier, usage=None):
        """Invoke Bedrock with Anthropic API format."""
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": content}],
        }
        resp = self._client().invoke_model(modelId=model_id, body=json.dumps(body))
        payload = json.loads(resp["body"].read())
        if usage is not None:
            u = payload.get("usage") or {}
            usage.add(
                u.get("input_tokens", 0),
                u.get("output_tokens", 0),
                provider="bedrock",
                tier=tier,
                model=model_id,
            )
        return strip_code_fence(payload["content"][0]["text"])

    def model_for_tier(self, tier):
        """Resolve tier to Bedrock model id."""
        if tier == "premium":
            return settings.BEDROCK_MODEL_ID
        return getattr(settings, "BEDROCK_MODEL_ID_CHEAP", settings.BEDROCK_MODEL_ID)

    def invoke_with_tools(
        self,
        system,
        content,
        max_tokens,
        model_id,
        tier,
        tools,
        usage=None,
        max_iterations=8,
    ):
        """Bedrock-native (Anthropic Messages) tool-use loop.

        Note: no `temperature` — newer Bedrock Claude models reject it.
        """
        from llm.tools import run_tool

        anthropic_tools = [t.to_anthropic() for t in tools]
        messages = [{"role": "user", "content": content}]

        for _ in range(max(1, max_iterations)):
            body = {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": max_tokens,
                "system": system,
                "tools": anthropic_tools,
                "messages": messages,
            }
            resp = self._client().invoke_model(modelId=model_id, body=json.dumps(body))
            payload = json.loads(resp["body"].read())
            if usage is not None:
                u = payload.get("usage") or {}
                usage.add(
                    u.get("input_tokens", 0),
                    u.get("output_tokens", 0),
                    provider="bedrock",
                    tier=tier,
                    model=model_id,
                )

            blocks = payload.get("content") or []
            if payload.get("stop_reason") != "tool_use":
                text = "".join(
                    b.get("text", "")
                    for b in blocks
                    if isinstance(b, dict) and b.get("type") == "text"
                ).strip()
                if not text:
                    raise RuntimeError("bedrock: tool loop returned empty content")
                return strip_code_fence(text)

            # Build the tool_results for each tool_use block.
            tool_results = []
            for b in blocks:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    result = run_tool(tools, b.get("name", ""), b.get("input") or {})
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": b.get("id", ""),
                            "content": result,
                        }
                    )
            # Guard: stop_reason said tool_use but no tool_use blocks were present.
            # Don't append an empty tool_results turn (Anthropic rejects it) — treat
            # any text in the reply as the final answer.
            if not tool_results:
                text = "".join(
                    b.get("text", "")
                    for b in blocks
                    if isinstance(b, dict) and b.get("type") == "text"
                ).strip()
                if text:
                    return strip_code_fence(text)
                raise RuntimeError(
                    "bedrock: tool_use stop with no tool calls and no text"
                )

            # Echo the assistant turn, then answer each tool_use with a tool_result.
            messages.append({"role": "assistant", "content": blocks})
            messages.append({"role": "user", "content": tool_results})

        raise RuntimeError(
            f"bedrock: exceeded {max_iterations} tool-use iterations "
            "without a final answer"
        )
