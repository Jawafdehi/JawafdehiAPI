"""Provider registry and tier-based routing."""

from django.conf import settings

_PROVIDERS = {}


def get_provider(name):
    """Get or instantiate a provider by name.

    Args:
        name: Provider name ("bedrock", "proxy", "codex_cli", "claude_cli")

    Returns:
        Provider instance

    Raises:
        RuntimeError: For unknown provider names
        NotImplementedError: For providers not yet implemented
    """
    name = name.strip().lower()

    if name in _PROVIDERS:
        return _PROVIDERS[name]

    if name == "bedrock":
        from llm.providers.bedrock import BedrockProvider

        provider = BedrockProvider()
    elif name == "proxy":
        from llm.providers.proxy import ProxyProvider

        provider = ProxyProvider()
    elif name == "codex_cli":
        from llm.providers.cli import CodexCliProvider

        provider = CodexCliProvider()
    elif name == "claude_cli":
        from llm.providers.cli import ClaudeCliProvider

        provider = ClaudeCliProvider()
    elif name == "claude_agent":
        from llm.providers.agent import ClaudeAgentProvider

        provider = ClaudeAgentProvider()
    else:
        raise RuntimeError(f"Unknown LLM provider: {name}")

    _PROVIDERS[name] = provider
    return provider


def provider_for_tier(tier):
    """Get the active provider for a tier.

    Args:
        tier: "premium" or "cheap"

    Returns:
        Provider instance
    """
    if tier == "premium":
        provider_name = settings.REVIEW_LLM_PROVIDER_PREMIUM
    else:
        provider_name = settings.REVIEW_LLM_PROVIDER_CHEAP
    return get_provider(provider_name)


def model_for_tier(tier):
    """Resolve a tier to the active provider's model id.

    Args:
        tier: "premium" or "cheap"

    Returns:
        Model identifier string
    """
    return provider_for_tier(tier).model_for_tier(tier)


def active_premium_model():
    """Get the premium-tier model id for the active provider.

    Useful for run reporting and diagnostics.

    Returns:
        Model identifier string
    """
    return model_for_tier("premium")
