"""LLM wiring onto the in-repo `llm/` package.

Ported enrichers route work through two "tiers": a `premium` tier for
extraction stages that need the strongest available model (bigo estimation,
timeline, allegations, entity linking) and a `cheap` tier for cheap/bulk
classification (tags). Production picks a real provider+model per tier via
`REVIEW_LLM_PROVIDER_PREMIUM` / `REVIEW_LLM_PROVIDER_CHEAP`. For local/dev A-B
runs we force BOTH tiers onto the same cheap local harness (`claude_cli` +
`haiku`) so a difference between two runs can't be explained away by "the
premium tier just has a better model" -- it isolates the enricher-logic
change under test.
"""

import os

# These values are NOT judgements about how expensive/cheap a stage "feels" --
# they mirror the donor's actual `tier=` arguments to `invoke_text`/
# `invoke_with_tools`, verified at donor commit 0321a85:
#   enrich_missing_bigo.py:446      tier="premium"
#   enrich_tags.py:1088             tier="cheap"
#   enrich_timeline.py:518          tier="premium"
#   enrich_allegations.py:350       tier="premium"
#   enrich_related_entities.py:421  tier="premium"
#   enrich_description.py:576       tier="premium"
#   enrich_card.py:337              tier="cheap"
TIERS = {
    "bigo": "premium",
    "tags": "cheap",
    "timeline": "premium",
    "allegations": "premium",
    "entities": "premium",
    "description": "premium",
    # `card` is cheap on the donor's own authority (enrich_card.py:337) and on
    # the merits: it fetches no source document, only summarising a
    # `description` already on the case. NOTE the donor's OTHER title writer,
    # the standalone `enrich_title.py`, used tier="premium" (line 275) for the
    # same no-fetch inputs -- the two donors disagreed. `enrich_title.py` folds
    # into this stage as `--only title`, and the brief resolves the conflict in
    # favour of cheap. See `casework/enrich_card.py`'s deviation 2.
    "card": "cheap",
    # `evidence_notes` is premium on the donor's own authority
    # (enrich_evidence.py:447 at 0321a85, tier="premium") and on the merits: it
    # reads the source DOCUMENT to say what the document is, which is the same
    # shape as `description`.
    "evidence_notes": "premium",
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


def bootstrap(provider="claude_cli", model="", dev=False):
    """Configure Django settings + llm env BEFORE importing llm.invoke.

    Must run before any `from llm.invoke import invoke_text` (or anything
    else that imports `config.settings`), since the env vars this sets are
    read at Django settings module-import time, not lazily.

    Args:
        provider: Provider name to route BOTH tiers to (default "claude_cli").
            Honoured whenever dev=False.
        model: Model id/alias to force on both tiers when non-empty.
        dev: When True, force claude_cli+haiku on both tiers via
            dev_env_overrides() *regardless of* `provider`/`model` -- the
            A/B-run override only. Defaults to False so `provider` is honoured:
            an earlier default of True silently ignored every enricher's
            `--provider`, pinning all runs to claude_cli. Callers that want the
            forced-cheap A/B behaviour must now opt in with dev=True.
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
