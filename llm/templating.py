# SPDX-License-Identifier: Hippocratic-3.0
"""Loading prompt text from template files.

Prompt text lives in ``<app>/prompt_templates/*.md`` rather than in Python
string constants, so it can be read, diffed and reviewed as prose instead of as
an f-string with the indentation fought into shape.

This uses Django's template engine, but a **dedicated instance** — not the one
in ``settings.TEMPLATES`` that renders HTML. Two of its defaults are actively
wrong for prompts, and both fail silently:

**Autoescaping must be off.** Precisely: Django escapes *interpolated values*,
not the literal text of a template — so the danger is not the prompt wording, it
is the data. And prompt context is exactly the wrong shape for it. The judge
path passes ``json.dumps(case_summary, indent=2)``, which under autoescaping
arrives at the model as a wall of ``&quot;`` instead of JSON; source excerpts
carry quotes and ampersands; case titles carry ``&``. The result is a prompt
that still looks like a prompt, produces plausible-but-wrong output, and raises
nothing.

(Literal prompt text is safe either way. That asymmetry is easy to test wrong —
a template whose HTML sits in the *body* passes with escaping switched on, which
is why the tests here put the hostile characters in the context values.)

**A missing variable must not render as empty.** Django's default is to swallow
an unknown variable and emit ``""``. For a prompt that means a renamed context
key silently ships a prompt with a hole in it and you get a bad answer rather
than a crash. So the engine is configured with a sentinel
``string_if_invalid``, and :func:`render_prompt` refuses to return any string
still containing it.

That sentinel closes the ``{{ var }}`` case but **not** the tag case: Django
never consults ``string_if_invalid`` for ``{% if missing %}`` (falsy) or
``{% for x in missing %}`` (empty). A variable used *only* inside a tag must
therefore be declared in ``required=``, which is checked before rendering.

One consequence of the sentinel worth knowing: ``{{ x|default:"unknown" }}``
does not rescue an *absent* ``x`` — Django substitutes the sentinel without
applying filters. ``default`` still works for a key that is present but empty,
which is the coherent reading anyway: pass the key, and let the filter handle
the empty case.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

from django.apps import apps
from django.template import Context, Engine, TemplateDoesNotExist

# Deliberately no logger here. Rendering happens on the way to a model call that
# llm.prompts already logs (name + version + size), and anything this module
# could usefully log is the prompt text itself — which carries case content and
# does not belong in the log stream.

#: Directory name, relative to an app package, holding that app's prompt
#: templates. NOT ``prompts``: ``llm/prompts.py`` is the registry module, and a
#: module and a package of the same name cannot coexist in one package.
PROMPT_DIR_NAME = "prompt_templates"

#: Emitted in place of an unresolvable variable. NUL-delimited so it cannot
#: collide with anything a real template or a real context value contains. The
#: ``%s`` is Django's own convention — it substitutes the variable's name, which
#: is what makes the resulting error actionable rather than just "something was
#: missing".
MISSING_SENTINEL = "\x00prompt-missing:%s\x00"

_MISSING_RE = re.compile("\x00prompt-missing:(.*?)\x00")

_engine: Engine | None = None


class PromptRenderError(Exception):
    """A prompt could not be rendered into text safe to send to a model."""


def prompt_template_dirs() -> list[Path]:
    """Every installed app's ``prompt_templates/`` directory that exists.

    Discovered from the app registry rather than hardcoded, so an app owns its
    own prompts and adding one is a directory, not a settings edit.
    """
    dirs = []
    for config in apps.get_app_configs():
        candidate = Path(config.path) / PROMPT_DIR_NAME
        if candidate.is_dir():
            dirs.append(candidate)
    return dirs


def get_engine() -> Engine:
    """The process-wide prompt engine, built on first use and cached.

    Built lazily because it reads the app registry, which is not populated at
    import time.
    """
    global _engine
    if _engine is None:
        _engine = Engine(
            dirs=[str(d) for d in prompt_template_dirs()],
            # Templates are found by explicit dirs only. app_dirs would look in
            # <app>/templates/, which is the HTML engine's territory.
            app_dirs=False,
            autoescape=False,
            string_if_invalid=MISSING_SENTINEL,
        )
    return _engine


def reset_engine() -> None:
    """Drop the cached engine so the next call rediscovers directories."""
    global _engine
    _engine = None


def render_prompt(
    template_name: str,
    context: dict[str, Any] | None = None,
    *,
    required: Iterable[str] = (),
) -> str:
    """Render ``template_name`` to prompt text.

    Args:
        template_name: Path relative to a ``prompt_templates/`` directory, e.g.
            ``"case_proposal/intent.content.md"``.
        context: Template context.
        required: Context keys that must be present. Needed for variables used
            only inside ``{% if %}`` / ``{% for %}``, where Django resolves a
            missing name to falsy without consulting ``string_if_invalid``.

    Returns:
        The rendered text, with trailing whitespace stripped.

    Raises:
        PromptRenderError: if the template is missing, a required key is absent,
            or any variable failed to resolve. Never returns a partially-filled
            prompt — sending one to a model is worse than failing, because the
            failure would surface later as a bad record with no obvious cause.
    """
    context = dict(context or {})

    missing_required = [key for key in required if key not in context]
    if missing_required:
        raise PromptRenderError(
            f"{template_name}: missing required context {sorted(missing_required)}. "
            f"Got {sorted(context)}."
        )

    engine = get_engine()
    try:
        template = engine.get_template(template_name)
    except TemplateDoesNotExist as exc:
        raise PromptRenderError(
            f"No prompt template {template_name!r} in any of "
            f"{[str(d) for d in prompt_template_dirs()]}. Prompt templates live in "
            f"<app>/{PROMPT_DIR_NAME}/ and the app must be in INSTALLED_APPS."
        ) from exc

    # autoescape is a property of the Context, not only of the Engine — building
    # a bare Context() here would re-enable escaping and quietly undo the point
    # of this module.
    rendered = template.render(Context(context, autoescape=engine.autoescape))

    unresolved = _MISSING_RE.findall(rendered)
    if unresolved:
        raise PromptRenderError(
            f"{template_name}: unresolved template variables {sorted(set(unresolved))}. "
            f"Context had {sorted(context)}. A prompt is never rendered with holes in it."
        )

    return rendered.rstrip()
