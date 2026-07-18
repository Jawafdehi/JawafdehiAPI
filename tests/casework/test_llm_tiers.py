from django.test import override_settings

from casework.common.llm import TIERS, dev_env_overrides, tier_for


def test_production_tiers_match_donor():
    assert tier_for("bigo") == "premium"
    assert tier_for("tags") == "cheap"
    assert tier_for("timeline") == "premium"


def test_unknown_stage_defaults_to_cheap():
    assert tier_for("nonexistent") == "cheap"


def test_dev_override_forces_haiku_on_both_tiers():
    """dev_env_overrides() must name the env vars the claude_cli provider
    ACTUALLY reads.

    ``llm/providers/cli.py::ClaudeCliProvider.model_for_tier`` reads
    ``settings.CLAUDE_CLI_MODEL_PREMIUM`` / ``CLAUDE_CLI_MODEL_CHEAP`` -- no
    ``_ID_`` infix, unlike the sibling ``BEDROCK_MODEL_ID_CHEAP`` /
    ``LLM_PROXY_MODEL_ID_CHEAP`` settings which DO use that infix. Asserting
    only that the returned dict equals a hand-built literal is toothless: it
    would still pass even if the keys named don't exist anywhere in
    settings.py (as an earlier draft of this override did). Route the
    override through ``override_settings`` and drive the real consumer chain
    (``llm.routing.model_for_tier`` -> ``provider_for_tier`` ->
    ``ClaudeCliProvider.model_for_tier``) to prove the values actually land
    where the provider looks for them.
    """
    env = dev_env_overrides()
    assert env["REVIEW_LLM_PROVIDER_PREMIUM"] == "claude_cli"
    assert env["REVIEW_LLM_PROVIDER_CHEAP"] == "claude_cli"

    from llm import routing

    with override_settings(**env):
        assert routing.model_for_tier("premium") == "haiku"
        assert routing.model_for_tier("cheap") == "haiku"


def test_tiers_cover_every_ported_stage():
    for stage in ("bigo", "tags", "timeline", "allegations", "entities"):
        assert stage in TIERS
