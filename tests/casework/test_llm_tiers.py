from django.test import override_settings

from casework.common.llm import TIERS, dev_env_overrides, tier_for


def test_production_tiers_match_donor():
    """Every ported stage's tier must match the donor's actual `tier=` argument,
    verified at donor commit 0321a85:

        enrich_missing_bigo.py:446      tier="premium"
        enrich_tags.py:1088             tier="cheap"
        enrich_timeline.py:518          tier="premium"
        enrich_allegations.py:350       tier="premium"
        enrich_related_entities.py:421  tier="premium"

    All five stages are asserted here (not just a subset) so that flipping
    any single stage's tier -- including "entities", which shipped wrong as
    "cheap" -- is caught. A previous version of this test only asserted
    "bigo"/"tags"/"timeline", which left "allegations" and "entities"
    unchecked; flipping "entities" to any other tier left it green.
    """
    assert tier_for("bigo") == "premium"
    assert tier_for("tags") == "cheap"
    assert tier_for("timeline") == "premium"
    assert tier_for("allegations") == "premium"
    assert tier_for("entities") == "premium"


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


def test_bootstrap_defaults_to_prod_and_honors_provider(monkeypatch):
    """bootstrap() defaults dev=False so the caller's provider is honoured.

    An earlier default of dev=True routed EVERY call through dev_env_overrides(),
    silently forcing claude_cli on both tiers -- so every enricher's --provider
    flag was a no-op. This pins that the default now respects provider/model.
    """
    import os

    import casework.common.llm as llm_mod

    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr("django.setup", lambda: None)

    llm_mod.bootstrap("bedrock", "some-model")  # no dev= -> must NOT force claude_cli

    assert os.environ["REVIEW_LLM_PROVIDER_PREMIUM"] == "bedrock"
    assert os.environ["REVIEW_LLM_PROVIDER_CHEAP"] == "bedrock"
    assert os.environ["CLAUDE_CLI_MODEL_PREMIUM"] == "some-model"


def test_bootstrap_dev_true_still_forces_claude_cli(monkeypatch):
    """dev=True remains an explicit opt-in that overrides provider (A/B runs)."""
    import os

    import casework.common.llm as llm_mod

    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr("django.setup", lambda: None)

    llm_mod.bootstrap("bedrock", "", dev=True)  # explicit opt-in wins over provider

    assert os.environ["REVIEW_LLM_PROVIDER_PREMIUM"] == "claude_cli"
    assert os.environ["CLAUDE_CLI_MODEL_PREMIUM"] == "haiku"


def test_evidence_notes_is_premium():
    """It reads the source document itself -- the same shape as `description`,
    and the brief registers it premium for that reason."""
    assert tier_for("evidence_notes") == "premium"
