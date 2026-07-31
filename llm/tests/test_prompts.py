# SPDX-License-Identifier: Hippocratic-3.0
"""Tests for the PromptSpec registry.

No DB and no model calls: every test either exercises validation or asserts on
what would have been passed to ``invoke_json``, which is mocked. The registry is
module-level global state, so tests that register anything clean up after
themselves.
"""

from unittest import mock

import pytest

from llm import prompts
from llm.prompts import PromptSpec


def _spec(**kw):
    """A valid spec, overridable per-test."""
    defaults = dict(
        name="test.spec",
        version=1,
        system="You reply with JSON.",
        build_content=lambda **kwargs: "content",
    )
    return PromptSpec(**{**defaults, **kw})


@pytest.fixture(autouse=True)
def _clean_registry():
    """Snapshot/restore the registry so tests can't leak into each other."""
    before = dict(prompts._REGISTRY)
    yield
    prompts._REGISTRY.clear()
    prompts._REGISTRY.update(before)


class TestValidation:
    def test_valid_spec_constructs(self):
        assert _spec().tier == "premium"

    def test_unknown_tier_raises(self):
        # The point of the guard: llm.routing.provider_for_tier resolves
        # "premium" and treats everything else as CHEAP, so a typo would
        # silently downgrade the model instead of failing.
        with pytest.raises(ValueError, match="unknown tier"):
            _spec(tier="premuim")

    def test_cheap_tier_is_allowed(self):
        assert _spec(tier="cheap").tier == "cheap"

    @pytest.mark.parametrize(
        "kwargs,match",
        [
            ({"name": ""}, "name is required"),
            ({"version": 0}, "version must be >= 1"),
            ({"system": "   "}, "system prompt is empty"),
            ({"max_tokens": 0}, "max_tokens must be >= 1"),
        ],
    )
    def test_rejects_bad_field(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            _spec(**kwargs)

    def test_spec_is_frozen(self):
        spec = _spec()
        with pytest.raises(Exception):
            spec.version = 2


class TestRegistry:
    def test_register_then_get(self):
        spec = prompts.register(_spec(name="a.b"))
        assert prompts.get("a.b") is spec

    def test_register_returns_the_spec(self):
        # So a module can do: SPEC = register(PromptSpec(...)) in one statement.
        spec = _spec(name="a.b")
        assert prompts.register(spec) is spec

    def test_register_replaces(self):
        prompts.register(_spec(name="a.b", version=1))
        prompts.register(_spec(name="a.b", version=2))
        assert prompts.get("a.b").version == 2

    def test_get_unknown_raises_with_the_known_names(self):
        prompts.register(_spec(name="a.b"))
        with pytest.raises(KeyError) as exc:
            prompts.get("nope")
        # The error should be actionable, not just "KeyError: 'nope'".
        assert "a.b" in str(exc.value)

    def test_known_is_sorted(self):
        prompts.register(_spec(name="z.z"))
        prompts.register(_spec(name="a.a"))
        assert prompts.known().index("a.a") < prompts.known().index("z.z")


class TestInvoke:
    def test_render_does_not_call_a_model(self):
        spec = _spec(build_content=lambda subject, **kw: f"about {subject}")
        with mock.patch("llm.prompts.invoke_json") as invoke:
            assert spec.render(subject="a docket") == "about a docket"
        invoke.assert_not_called()

    def test_invoke_passes_system_content_and_params(self):
        spec = _spec(
            system="SYS",
            build_content=lambda subject, **kw: f"about {subject}",
            tier="cheap",
            max_tokens=321,
        )
        with mock.patch("llm.prompts.invoke_json", return_value={"ok": True}) as invoke:
            out = spec.invoke(subject="a docket")

        assert out == {"ok": True}
        args, kwargs = invoke.call_args
        assert args == ("SYS", "about a docket")
        assert kwargs["tier"] == "cheap"
        assert kwargs["max_tokens"] == 321

    def test_invoke_forwards_usage(self):
        spec = _spec()
        sentinel = object()
        with mock.patch("llm.prompts.invoke_json", return_value={}) as invoke:
            spec.invoke(usage=sentinel)
        assert invoke.call_args.kwargs["usage"] is sentinel

    def test_usage_is_not_passed_to_build_content(self):
        # `usage` is an invoke_json concern; a content builder should never see it.
        seen = {}

        def build(**kwargs):
            seen.update(kwargs)
            return "c"

        with mock.patch("llm.prompts.invoke_json", return_value={}):
            _spec(build_content=build).invoke(usage=object(), subject="x")

        assert seen == {"subject": "x"}

    def test_invoke_logs_name_and_version(self):
        # "Which prompt version produced this?" must be answerable from logs.
        spec = _spec(name="case_proposal.intent", version=7)
        with mock.patch("llm.prompts.invoke_json", return_value={}):
            with mock.patch.object(prompts.logger, "info") as log:
                spec.invoke()

        kwargs = log.call_args.kwargs
        assert kwargs["prompt"] == "case_proposal.intent"
        assert kwargs["version"] == 7
