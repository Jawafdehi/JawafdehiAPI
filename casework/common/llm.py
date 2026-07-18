"""LLM wiring onto the in-repo `llm/` package.

Ported enrichers route work through two "tiers": a `premium` tier for
extraction stages that need the strongest available model (bigo estimation,
timeline, allegations) and a `cheap` tier for cheap/bulk classification
(tags, entity linking). Production picks a real provider+model per tier via
`REVIEW_LLM_PROVIDER_PREMIUM` / `REVIEW_LLM_PROVIDER_CHEAP`. For local/dev A-B
runs we force BOTH tiers onto the same cheap local harness (`claude_cli` +
`haiku`) so a difference between two runs can't be explained away by "the
premium tier just has a better model" -- it isolates the enricher-logic
change under test.
"""

import os

TIERS = {
    "bigo": "premium",
    "tags": "cheap",
    "timeline": "premium",
    "allegations": "premium",
    "entities": "cheap",
}
DEFAULT_TIER = "cheap"


def tier_for(stage):
    """Resolve a pipeline stage name to its LLM tier ("premium"/"cheap").

    Unknown stages default to "cheap" -- the safe/cautious side, since an
    unrecognised stage should not silently start consuming premium quota.
    """
    return TIERS.get(stage, DEFAULT_TIER)


def dev_env_overrides(model="haiku"):
    """Env overrides that force claude_cli+haiku on BOTH tiers for A/B runs.

    Uses the settings names the claude_cli provider actually reads --
    ``llm/providers/cli.py::ClaudeCliProvider.model_for_tier`` resolves
    ``settings.CLAUDE_CLI_MODEL_PREMIUM`` / ``CLAUDE_CLI_MODEL_CHEAP``. Note
    this pair has NO ``_ID_`` infix, unlike the sibling
    ``BEDROCK_MODEL_ID_CHEAP`` / ``LLM_PROXY_MODEL_ID_CHEAP`` settings which
    do -- an inconsistency in the shared `llm/` app's own naming, not a typo
    here. Do not "fix" these names to match that other pattern without also
    changing `config/settings.py` (out of scope for this package).
    """
    return {
        "REVIEW_LLM_PROVIDER_PREMIUM": "claude_cli",
        "REVIEW_LLM_PROVIDER_CHEAP": "claude_cli",
        "CLAUDE_CLI_MODEL_PREMIUM": model,
        "CLAUDE_CLI_MODEL_CHEAP": model,
    }


def bootstrap(provider="claude_cli", model="", dev=True):
    """Configure Django settings + llm env BEFORE importing llm.invoke.

    Must run before any `from llm.invoke import invoke_text` (or anything
    else that imports `config.settings`), since the env vars this sets are
    read at Django settings module-import time, not lazily.

    Args:
        provider: Provider name to use when dev=False (default "claude_cli").
        model: Model id/alias to force when dev=False and non-empty.
        dev: When True (default), force claude_cli+haiku on both tiers via
            dev_env_overrides() regardless of `provider`/`model`.
    """
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if dev:
        os.environ.update(dev_env_overrides(model or "haiku"))
    else:
        os.environ["REVIEW_LLM_PROVIDER_PREMIUM"] = provider
        os.environ["REVIEW_LLM_PROVIDER_CHEAP"] = provider
        if model:
            os.environ["CLAUDE_CLI_MODEL_PREMIUM"] = model
            os.environ["CLAUDE_CLI_MODEL_CHEAP"] = model
    import django

    django.setup()
