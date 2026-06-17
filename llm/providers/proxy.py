"""OpenAI-compatible llm-proxy provider."""

from django.conf import settings

from llm.providers.base import Provider, strip_code_fence

_proxy = None


class ProxyProvider(Provider):
    """LLM provider using in-house OpenAI-compatible llm-proxy."""

    name = "proxy"

    def _client(self):
        """Singleton OpenAI client pointed at the in-house llm-proxy.

        The `openai` import is deferred to here — the default Bedrock path
        never imports it.
        """
        global _proxy
        if _proxy is None:
            from openai import OpenAI

            kwargs = dict(
                base_url=settings.LLM_PROXY_BASE_URL,
                api_key=settings.LLM_PROXY_API_KEY or "unused",
                timeout=120,
                max_retries=4,
            )
            # The public llm-proxy host is behind a Cloudflare WAF that 403s the
            # OpenAI SDK's default User-Agent. Override it when fronting the proxy
            # publicly (not needed for the in-cluster ClusterIP path).
            ua = getattr(settings, "LLM_PROXY_USER_AGENT", "")
            if ua:
                kwargs["default_headers"] = {"User-Agent": ua}
            _proxy = OpenAI(**kwargs)
        return _proxy

    def invoke_text(self, system, content, max_tokens, model_id, tier, usage=None):
        """Invoke llm-proxy via OpenAI chat-completions API.

        Translates Anthropic-shaped content (string or text blocks) into OpenAI
        content format. Anthropic cache_control markers are dropped as the proxy
        fronts reasoning models that cache automatically.
        """
        if isinstance(content, str):
            user_content = content
        else:
            user_content = [{"type": "text", "text": b["text"]} for b in content]

        headroom = int(getattr(settings, "LLM_PROXY_REASONING_HEADROOM", 0))
        resp = self._client().chat.completions.create(
            model=model_id,
            max_tokens=max_tokens + headroom,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
        )
        if usage is not None and resp.usage is not None:
            usage.add(
                getattr(resp.usage, "prompt_tokens", 0) or 0,
                getattr(resp.usage, "completion_tokens", 0) or 0,
                provider="proxy",
                tier=tier,
                model=model_id,
            )
        return strip_code_fence(resp.choices[0].message.content or "")

    def model_for_tier(self, tier):
        """Resolve tier to proxy model id."""
        if tier == "premium":
            model = settings.LLM_PROXY_MODEL_ID
        else:
            model = (
                getattr(settings, "LLM_PROXY_MODEL_ID_CHEAP", "")
                or settings.LLM_PROXY_MODEL_ID
            )
        if not model:
            raise RuntimeError(
                "REVIEW_LLM_PROVIDER=proxy but LLM_PROXY_MODEL_ID is not set"
            )
        return model
