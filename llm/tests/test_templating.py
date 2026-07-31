# SPDX-License-Identifier: Hippocratic-3.0
"""Tests for the prompt template engine.

The load-bearing ones are the two silent-corruption guards: that autoescaping
is off, and that an unresolved variable raises instead of rendering as empty.
Both failure modes produce a prompt that still *looks* like a prompt, so
nothing downstream would notice — the model would just answer a slightly
different question and we would blame the model.
"""

import pytest

from llm import templating
from llm.templating import PromptRenderError, render_prompt

REFERENCE_SYSTEM = "reference/system.md"
REFERENCE_CONTENT = "reference/content.md"


@pytest.fixture(autouse=True)
def _reset_engine():
    """The engine is a cached module global; don't leak one test's dirs."""
    templating.reset_engine()
    yield
    templating.reset_engine()


@pytest.fixture
def templates(tmp_path, monkeypatch):
    """Render from a throwaway directory instead of a real app's."""

    def write(name, body):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    monkeypatch.setattr(templating, "prompt_template_dirs", lambda: [tmp_path])
    templating.reset_engine()
    return write


class TestAutoescapeIsOff:
    """Prompt *data* routinely contains the characters autoescaping mangles.

    Every test here puts the hostile characters in a context VALUE, never in
    the template body. Django only escapes interpolated values, so a test with
    HTML in the body passes with escaping switched on and proves nothing —
    these all fail if the engine is ever misconfigured.
    """

    def test_json_context_survives_intact(self, templates):
        # The realistic case: review/judge.py hands the model
        # json.dumps(case_summary, indent=2). Escaped, that reaches the model as
        # a wall of &quot; and stops being JSON.
        import json

        templates("t.md", "CASE DATA:\n{{ case_json }}")
        case_json = json.dumps({"title": "Lalita Niwas", "amount": "NPR 10,00,000"}, indent=2)
        assert render_prompt("t.md", {"case_json": case_json}).endswith(case_json)

    def test_quotes_in_a_value_survive(self, templates):
        templates("t.md", 'Reply EXACTLY: {"note": "{{ note }}"}')
        assert render_prompt("t.md", {"note": 'he said "no"'}) == (
            'Reply EXACTLY: {"note": "he said "no""}'
        )

    def test_html_in_a_value_survives(self, templates):
        templates("t.md", "EXCERPT:\n{{ excerpt }}")
        excerpt = "<p>The <strong>accused</strong> denied it.</p>"
        assert render_prompt("t.md", {"excerpt": excerpt}) == f"EXCERPT:\n{excerpt}"

    def test_ampersands_and_angle_brackets_in_context_survive(self, templates):
        # The value, not just the template, must come through unescaped.
        templates("t.md", "{{ v }}")
        assert render_prompt("t.md", {"v": "R&D <tag> 'quoted'"}) == "R&D <tag> 'quoted'"

    def test_devanagari_survives(self, templates):
        templates("t.md", "{{ v }}")
        agency = "अख्तियार दुरुपयोग अनुसन्धान आयोग"
        assert render_prompt("t.md", {"v": agency}) == agency


class TestMissingVariablesRaise:
    def test_unresolved_variable_raises_and_names_it(self, templates):
        templates("t.md", "Case: {{ case_title }}")
        with pytest.raises(PromptRenderError) as exc:
            render_prompt("t.md", {})
        assert "case_title" in str(exc.value)

    def test_the_sentinel_never_leaks_into_output(self, templates):
        templates("t.md", "Case: {{ nope }}")
        with pytest.raises(PromptRenderError):
            render_prompt("t.md", {})

    def test_unresolved_dotted_path_raises(self, templates):
        templates("t.md", "{{ case.title }}")
        with pytest.raises(PromptRenderError, match="case.title"):
            render_prompt("t.md", {"case": {}})

    def test_present_but_empty_is_not_missing(self, templates):
        # An empty string is a legitimate value; only absence is an error.
        templates("t.md", "[{{ v }}]")
        assert render_prompt("t.md", {"v": ""}) == "[]"

    def test_required_key_absent_raises_before_rendering(self, templates):
        templates("t.md", "{% if flag %}on{% else %}off{% endif %}")
        with pytest.raises(PromptRenderError, match="flag"):
            render_prompt("t.md", {}, required=["flag"])

    def test_required_closes_the_tag_shaped_hole(self, templates):
        # Django resolves a missing name inside {% if %} / {% for %} to falsy
        # WITHOUT consulting string_if_invalid, so the sentinel cannot see it.
        # This test documents that gap and proves `required` is what covers it.
        templates("t.md", "{% if flag %}on{% else %}off{% endif %}")
        assert render_prompt("t.md", {}) == "off"  # silently wrong, and allowed
        with pytest.raises(PromptRenderError):
            render_prompt("t.md", {}, required=["flag"])

    def test_missing_template_names_the_search_path(self, templates):
        templates("other.md", "x")
        with pytest.raises(PromptRenderError) as exc:
            render_prompt("absent.md", {})
        assert "absent.md" in str(exc.value)
        assert "prompt_templates" in str(exc.value)


class TestRendering:
    def test_loops_and_conditionals_shape_data_into_prose(self, templates):
        templates(
            "t.md",
            "{% for r in rules %}- {{ r.title }}\n{% empty %}none\n{% endfor %}",
        )
        out = render_prompt("t.md", {"rules": [{"title": "A"}, {"title": "B"}]})
        assert out == "- A\n- B"

    def test_comments_are_stripped(self, templates):
        templates("t.md", "{# a note for humans #}text")
        assert render_prompt("t.md", {}) == "text"

    def test_trailing_whitespace_is_stripped(self, templates):
        templates("t.md", "text\n\n\n")
        assert render_prompt("t.md", {}) == "text"


class TestDiscovery:
    def test_finds_the_llm_apps_prompt_templates_directory(self):
        # No monkeypatching here: this asserts the real on-disk convention works.
        dirs = [str(d) for d in templating.prompt_template_dirs()]
        assert any(d.endswith("llm/prompt_templates") for d in dirs), dirs

    def test_the_engine_is_not_the_html_engine(self):
        from django.template.loader import engines

        assert templating.get_engine().autoescape is False
        # The configured HTML engine still escapes; we did not change it.
        assert engines["django"].engine.autoescape is True


class TestTheReferenceTemplates:
    """The shipped reference pair renders, and proves the guards end to end."""

    def test_content_renders_with_excerpts(self):
        out = render_prompt(
            REFERENCE_CONTENT,
            {"case_title": "Lalita Niwas", "excerpts": ["first", "second"]},
        )
        assert "CASE: Lalita Niwas" in out
        assert "- first" in out and "- second" in out
        # The JSON shape and the HTML tag both survive unescaped.
        assert '{"summary": "<str>", "confidence": <float 0-1>}' in out
        assert "<strong>" in out

    def test_content_renders_without_excerpts(self):
        out = render_prompt(REFERENCE_CONTENT, {"case_title": "X", "excerpts": []})
        assert "No source excerpts were available." in out

    def test_system_switches_language_and_keeps_devanagari(self):
        assert "नेपाली भाषामा" in render_prompt(REFERENCE_SYSTEM, {"language": "np"})
        assert "Reply in English." in render_prompt(REFERENCE_SYSTEM, {"language": "en"})

    def test_content_still_fails_loudly_without_its_variable(self):
        with pytest.raises(PromptRenderError, match="case_title"):
            render_prompt(REFERENCE_CONTENT, {"excerpts": []})
