# SPDX-License-Identifier: Hippocratic-3.0
"""A named, versioned registry of prompts backed by template files.

Prompts in this codebase are conventionally a module-level system constant plus
a ``_build_*`` function that assembles the content block, handed to
:func:`llm.invoke.invoke_json` with a tier and a token budget (see
``review/judge.py``). That works, but it leaves the prompt anonymous — when an
LLM-produced record later turns out to be wrong, "which prompt produced this,
and has it changed since?" has no answer — and it leaves the text itself buried
in Python, where reviewing a wording change means reading a diff of an f-string.

A :class:`PromptSpec` is a name, a version, two template files (system and
content) and the parameters to invoke them with. The text lives in
``<app>/prompt_templates/`` and renders through :mod:`llm.templating`, which is
a dedicated non-autoescaping, strict-variable engine — see that module for why
the HTML engine would silently corrupt a prompt.

Templates hold *text*, not computation. Anything that needs Python — a
``json.dumps``, a character cap, a settings lookup — is done by the caller and
passed in as context. ``{% for %}`` and ``{% if %}`` are available for shaping
that data into prose, and that is the intended limit.

This is deliberately NOT a DB-backed CMS and not an abstraction over
:mod:`llm.invoke`; ``invoke_json`` remains the thing that talks to a model, and
a spec just remembers what to pass it.

Migrating ``review/judge.py`` and the ``casework/enrich_*`` prompts onto this is
explicitly out of scope for now: they predate the registry, they work, and
several of their tests assert on the Python constants directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

from llm.invoke import invoke_json
from llm.templating import render_prompt

logger = structlog.get_logger(__name__)

#: The tiers ``llm.routing`` knows about. Worth spelling out because
#: ``provider_for_tier`` resolves "premium" and treats EVERYTHING ELSE as cheap —
#: so a typo ("premuim", "strong") silently downgrades the model rather than
#: raising. Specs that require a strong model would fail quietly and
#: intermittently, which is the worst way for this to go wrong. Hence the
#: validation in :meth:`PromptSpec.__post_init__`.
TIERS = ("premium", "cheap")


@dataclass(frozen=True)
class PromptSpec:
    """One named, versioned prompt and the parameters it is invoked with.

    Args:
        name: Stable dotted identifier, e.g. ``"case_proposal.intent"``. This is
            what gets logged alongside the result, so it should not change once
            anything has been produced under it.
        version: Bump on ANY wording change, including edits to the template
            files. Recorded with every invocation so a bad output can be traced
            back to the exact prompt that produced it.
        system_template: Path to the system-prompt template, relative to a
            ``prompt_templates/`` directory.
        content_template: Path to the user-content template.
        tier: ``"premium"`` or ``"cheap"``. Validated, because a wrong value
            downgrades silently rather than raising.
        max_tokens: Response budget handed to ``invoke_json``.
        required: Context keys that must be present when rendering. Only needed
            for variables used *exclusively* inside ``{% if %}`` / ``{% for %}``,
            which Django resolves to falsy without flagging them as missing —
            plain ``{{ var }}`` holes are caught automatically.
    """

    name: str
    version: int
    system_template: str
    content_template: str
    tier: str = "premium"
    max_tokens: int = 1500
    required: tuple[str, ...] = ()

    def __post_init__(self):
        if not self.name:
            raise ValueError("PromptSpec.name is required.")
        if self.version < 1:
            raise ValueError(f"{self.name}: version must be >= 1, got {self.version!r}.")
        if not self.system_template.strip():
            raise ValueError(f"{self.name}: system_template is required.")
        if not self.content_template.strip():
            raise ValueError(f"{self.name}: content_template is required.")
        if self.tier not in TIERS:
            raise ValueError(
                f"{self.name}: unknown tier {self.tier!r}. Known: {list(TIERS)}. "
                "An unknown tier would route to the CHEAP model without raising."
            )
        if self.max_tokens < 1:
            raise ValueError(f"{self.name}: max_tokens must be >= 1, got {self.max_tokens!r}.")

    def render_system(self, **context) -> str:
        """Render the system prompt.

        Takes context too: a system prompt is often parameterised (the case
        scraper's switches its whole output language on one flag), and forcing
        that into the content block would put it further from the instruction it
        modifies.

        ``required`` applies here as well as to :meth:`render`. It did not, and
        that was the more dangerous half: the shipped reference system prompt
        branches on ``{% if language == "np" %}``, a tag-shaped hole the sentinel
        cannot see, so omitting ``language`` silently produced an English prompt
        with no error. Both templates take the same context, so one declaration
        covering both is also the less surprising rule.
        """
        return render_prompt(self.system_template, context, required=self.required)

    def render(self, **context) -> str:
        """Render the content block without invoking a model.

        The seam tests and prompt-review tooling hang off: it makes the exact
        text sent to the model assertable without spending a call.
        """
        return render_prompt(self.content_template, context, required=self.required)

    def invoke(self, usage=None, **context) -> Any:
        """Render both templates and invoke the model, returning parsed JSON.

        Thin by design — ``invoke_json`` already salvages dirty/truncated output,
        and re-implementing any of that here would put two behaviours in the
        codebase where callers expect one.

        Both templates get the same context, so a value needed by each is passed
        once.

        Args:
            usage: Optional UsageAccumulator, forwarded to ``invoke_json``.
            **context: Template context.

        Returns:
            Parsed JSON (dict or list), per ``invoke_json``.

        Raises:
            llm.templating.PromptRenderError: raised BEFORE any model call if
                either template is missing or a variable did not resolve, so a
                broken prompt costs nothing.
        """
        system = self.render_system(**context)
        content = self.render(**context)
        # Logged BEFORE the call as well as after, so a spec that reliably times
        # out or blows its token budget is still attributable to a version.
        logger.info(
            "prompt.invoke",
            prompt=self.name,
            version=self.version,
            tier=self.tier,
            max_tokens=self.max_tokens,
            content_chars=len(content),
        )
        return invoke_json(
            system,
            content,
            max_tokens=self.max_tokens,
            tier=self.tier,
            usage=usage,
        )


_REGISTRY: dict[str, PromptSpec] = {}


def register(spec: PromptSpec) -> PromptSpec:
    """Register ``spec`` under its name, replacing any existing entry.

    Replacement is allowed because registration happens at import time and
    modules can be re-imported (notably under the test runner), so a strict
    "already registered" error would be a false alarm far more often than a real
    catch. Returns the spec so it can be assigned at module level in one line.
    """
    _REGISTRY[spec.name] = spec
    return spec


def get(name: str) -> PromptSpec:
    """Return the spec registered under ``name``.

    Raises:
        KeyError: if nothing is registered under that name.

    Note this deliberately differs from ``jobs.registry.get``, which returns a
    default spec for an unregistered kind. A job kind has a sensible default
    policy; a prompt does not — there is no "default prompt", and silently
    invoking the wrong text is worse than failing. So this raises.
    """
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"No prompt registered as {name!r}. Registered: {known()}. "
            "Prompts register at import time — check the owning app is in "
            "INSTALLED_APPS and its registration module is imported."
        ) from None


def known() -> list[str]:
    """Every registered prompt name, sorted."""
    return sorted(_REGISTRY)


def all_specs() -> list[PromptSpec]:
    """Every registered spec, ordered by name."""
    return [_REGISTRY[name] for name in known()]
