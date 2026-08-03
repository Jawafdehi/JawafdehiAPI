# SPDX-License-Identifier: Hippocratic-3.0
"""Tests for the PromptSpec registry.

No DB and no model calls: every test either exercises validation or asserts on
what would have been passed to ``invoke_json``, which is mocked. The registry is
module-level global state, so tests that register anything clean up after
themselves.

Rendering itself is covered in test_templating.py; here it only matters that a
spec wires the right template to the right invoke parameters.
"""

import dataclasses
from unittest import mock

import pytest

from llm import prompts, templating
from llm.prompts import PromptSpec
from llm.templating import PromptRenderError

REFERENCE_SYSTEM = "reference/system.md"
REFERENCE_CONTENT = "reference/content.md"


def _spec(**kw):
    """A valid spec, overridable per-test."""
    defaults = dict(
        name="test.spec",
        version=1,
        system_template=REFERENCE_SYSTEM,
        content_template=REFERENCE_CONTENT,
    )
    return PromptSpec(**{**defaults, **kw})


@pytest.fixture(autouse=True)
def _clean_registry():
    """Snapshot/restore the registry so tests can't leak into each other."""
    before = dict(prompts._REGISTRY)
    yield
    prompts._REGISTRY.clear()
    prompts._REGISTRY.update(before)


@pytest.fixture(autouse=True)
def _reset_engine():
    templating.reset_engine()
    yield
    templating.reset_engine()


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
            ({"system_template": "  "}, "system_template is required"),
            ({"content_template": ""}, "content_template is required"),
            ({"max_tokens": 0}, "max_tokens must be >= 1"),
        ],
    )
    def test_rejects_bad_field(self, kwargs, match):
        with pytest.raises(ValueError, match=match):
            _spec(**kwargs)

    def test_spec_is_frozen(self):
        # FrozenInstanceError specifically: `Exception` also passes for a typo'd
        # attribute name on any frozen dataclass, so it would keep passing while
        # asserting nothing about `version` being a real field.
        spec = _spec()
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.version = 2
        assert "version" in {f.name for f in dataclasses.fields(spec)}

    def test_a_nonexistent_template_is_not_caught_until_render(self):
        # Construction cannot check the filesystem: specs are built at import
        # time, before the app registry the loader dirs come from is populated.
        spec = _spec(content_template="no/such.md")
        with pytest.raises(PromptRenderError, match="no/such.md"):
            spec.render(case_title="X", excerpts=[])


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

    def test_all_specs_follows_known_order(self):
        prompts._REGISTRY.clear()
        prompts.register(_spec(name="z.z"))
        prompts.register(_spec(name="a.a"))
        assert [s.name for s in prompts.all_specs()] == ["a.a", "z.z"]


class TestInvoke:
    def test_render_does_not_call_a_model(self):
        with mock.patch("llm.prompts.invoke_json") as invoke:
            out = _spec().render(case_title="Lalita Niwas", excerpts=[])
        assert "CASE: Lalita Niwas" in out
        invoke.assert_not_called()

    def test_invoke_passes_rendered_system_content_and_params(self):
        spec = _spec(tier="cheap", max_tokens=321)
        with mock.patch("llm.prompts.invoke_json", return_value={"ok": True}) as invoke:
            out = spec.invoke(case_title="Lalita Niwas", excerpts=["e1"], language="en")

        assert out == {"ok": True}
        system, content = invoke.call_args.args
        assert "Reply in English." in system
        assert "CASE: Lalita Niwas" in content
        assert "- e1" in content
        assert invoke.call_args.kwargs["tier"] == "cheap"
        assert invoke.call_args.kwargs["max_tokens"] == 321

    def test_both_templates_receive_the_same_context(self):
        # One context feeds system and content, so a shared value is passed once.
        with mock.patch("llm.prompts.invoke_json", return_value={}) as invoke:
            _spec().invoke(case_title="X", excerpts=[], language="np")
        system, _content = invoke.call_args.args
        assert "नेपाली भाषामा" in system

    def test_invoke_forwards_usage(self):
        sentinel = object()
        with mock.patch("llm.prompts.invoke_json", return_value={}) as invoke:
            _spec().invoke(usage=sentinel, case_title="X", excerpts=[])
        assert invoke.call_args.kwargs["usage"] is sentinel

    def test_usage_is_not_treated_as_template_context(self, tmp_path, monkeypatch):
        """`usage` is an invoke_json concern and must not reach the template.

        Asserting ``"usage" not in content`` against the reference template was
        vacuous — that template never mentions ``usage``, so the assertion held
        no matter what the context contained. This renders a template that
        *would* show it, so the test can actually fail.
        """
        (tmp_path / "probe.md").write_text("[{{ usage }}]", encoding="utf-8")
        monkeypatch.setattr(templating, "prompt_template_dirs", lambda: [tmp_path])
        templating.reset_engine()

        spec = _spec(system_template="probe.md", content_template="probe.md")
        with mock.patch("llm.prompts.invoke_json", return_value={}):
            # `usage` binds the parameter, so the template variable is missing —
            # which the sentinel catches. That is the proof it never arrived.
            with pytest.raises(PromptRenderError, match="usage"):
                spec.invoke(usage=object())

    def test_a_render_failure_costs_no_model_call(self):
        # The whole reason rendering is strict: fail before spending a call,
        # not after producing a record from a prompt with a hole in it.
        with mock.patch("llm.prompts.invoke_json") as invoke:
            with pytest.raises(PromptRenderError):
                _spec().invoke(excerpts=[])
        invoke.assert_not_called()

    def test_invoke_logs_name_and_version(self):
        # "Which prompt version produced this?" must be answerable from logs.
        spec = _spec(name="case_proposal.intent", version=7)
        with mock.patch("llm.prompts.invoke_json", return_value={}):
            with mock.patch.object(prompts.logger, "info") as log:
                spec.invoke(case_title="X", excerpts=[])

        kwargs = log.call_args.kwargs
        assert kwargs["prompt"] == "case_proposal.intent"
        assert kwargs["version"] == 7


class TestTheBudgetCanBeOverriddenForOneCall:
    """The seam an escalated retry hangs off (case_proposals.job_handlers)."""

    def test_the_specs_budget_is_used_when_nothing_is_passed(self):
        spec = _spec(max_tokens=8000)
        with mock.patch("llm.prompts.invoke_json", return_value={}) as invoke:
            spec.invoke(case_title="X", excerpts=[])
        assert invoke.call_args.kwargs["max_tokens"] == 8000

    def test_an_override_reaches_the_provider(self):
        spec = _spec(max_tokens=8000)
        with mock.patch("llm.prompts.invoke_json", return_value={}) as invoke:
            spec.invoke(case_title="X", excerpts=[], max_tokens=32_000)
        assert invoke.call_args.kwargs["max_tokens"] == 32_000

    def test_the_override_does_not_mutate_the_registered_spec(self):
        """Specs are frozen, and a retry must not widen the budget for everyone."""
        spec = _spec(max_tokens=8000)
        with mock.patch("llm.prompts.invoke_json", return_value={}):
            spec.invoke(case_title="X", excerpts=[], max_tokens=32_000)
        assert spec.max_tokens == 8000

    @pytest.mark.parametrize("bad", [0, -1, -32_000])
    def test_a_non_positive_override_is_refused_before_any_call(self, bad):
        """__post_init__ guards the configured budget; the override was the way
        past it. A zero budget still reaches the provider and still bills a call,
        and cannot produce a token — the pathological form of the very bug this
        seam was added to fix."""
        spec = _spec(max_tokens=8000)
        with mock.patch("llm.prompts.invoke_json") as invoke:
            with pytest.raises(ValueError, match="max_tokens override"):
                spec.invoke(case_title="X", excerpts=[], max_tokens=bad)
        invoke.assert_not_called()

    def test_the_log_records_the_effective_budget_not_the_specs(self):
        """Otherwise an escalated retry is indistinguishable in the logs from the
        attempt that provoked it, and a spec that escalates every single time
        looks exactly like one that never does."""
        spec = _spec(max_tokens=8000)
        with mock.patch("llm.prompts.invoke_json", return_value={}):
            with mock.patch.object(prompts.logger, "info") as log:
                spec.invoke(case_title="X", excerpts=[], max_tokens=32_000)

        kwargs = log.call_args.kwargs
        assert kwargs["max_tokens"] == 32_000
        assert kwargs["escalated"] is True

    def test_an_unescalated_call_says_so(self):
        spec = _spec(max_tokens=8000)
        with mock.patch("llm.prompts.invoke_json", return_value={}):
            with mock.patch.object(prompts.logger, "info") as log:
                spec.invoke(case_title="X", excerpts=[])
        assert log.call_args.kwargs["escalated"] is False


class TestRequiredAppliesToBothTemplates:
    """`required` must cover the system prompt too, not just the content.

    It previously covered only the content template, which is the less dangerous
    half: the shipped reference system prompt gates its whole output language on
    `{% if language == "np" %}`, a tag-shaped hole the sentinel cannot detect.
    """

    def test_render_system_enforces_required(self):
        spec = _spec(required=("language",))
        with pytest.raises(PromptRenderError, match="language"):
            spec.render_system()

    def test_render_system_succeeds_once_declared_key_is_supplied(self):
        spec = _spec(required=("language",))
        assert "नेपाली भाषामा" in spec.render_system(language="np")

    def test_invoke_refuses_before_calling_the_model(self):
        spec = _spec(required=("language",))
        with mock.patch("llm.prompts.invoke_json") as invoke:
            with pytest.raises(PromptRenderError, match="language"):
                spec.invoke(case_title="X", excerpts=[])
        invoke.assert_not_called()

    def test_render_still_enforces_required_for_content(self):
        spec = _spec(required=("flag",))
        with pytest.raises(PromptRenderError, match="flag"):
            spec.render(case_title="X", excerpts=[])


class TestRegisteredSpecsAreLoadable:
    def test_every_registered_spec_has_templates_that_exist(self):
        """Catches a spec pointing at a template that was renamed or never shipped.

        **Vacuous as of this commit**, and deliberately kept anyway: nothing
        registers a spec until the enrichment consumers land, so ``all_specs()``
        is empty and this loop does nothing. It is a forward guard that starts
        working the moment the first real spec is registered.

        What carries the weight *today* is ``TestTheReferenceTemplates``, which
        renders actual files off disk through the engine — that is what would
        fail if prompt templates were excluded from the wheel or the image, the
        same class of omission that already shipped a missing app directory once.
        """
        engine = templating.get_engine()
        for spec in prompts.all_specs():
            for template in (spec.system_template, spec.content_template):
                engine.get_template(template)  # raises TemplateDoesNotExist
