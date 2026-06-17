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
