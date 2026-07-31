# SPDX-License-Identifier: Hippocratic-3.0
"""A named, versioned registry of prompts.

Prompts in this codebase are conventionally a module-level system constant plus
a ``_build_*`` function that assembles the content block, handed to
:func:`llm.invoke.invoke_json` with a tier and a token budget (see
``review/judge.py``). That works, but it leaves the prompt anonymous: when an
LLM-produced record later turns out to be wrong, "which prompt produced this,
and has it changed since?" has no answer.

A :class:`PromptSpec` is those same three things made addressable — system text,
a content builder, and the invoke parameters — under a name and a version. That
is the whole scope. It is deliberately NOT a templating engine, not a
DB-backed CMS, and not an abstraction over :mod:`llm.invoke`; ``invoke_json``
remains the thing that talks to a model, and a spec just remembers what to pass
it.

Migrating ``review/judge.py`` onto this is explicitly out of scope: it predates
the registry, works, and its prompts are assembled per-rule in ways a single
spec doesn't model well.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import structlog

from llm.invoke import invoke_json

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
        version: Bump on ANY wording change. Recorded with every invocation so a
            bad output can be traced back to the exact prompt that produced it.
        system: The system prompt text.
        build_content: Callable assembling the user content block. Receives the
            keyword arguments passed to :meth:`render` / :meth:`invoke`.
        tier: ``"premium"`` or ``"cheap"``. Validated, because a wrong value
            downgrades silently rather than raising.
        max_tokens: Response budget handed to ``invoke_json``.
    """

    name: str
    version: int
    system: str
    build_content: Callable[..., str]
    tier: str = "premium"
    max_tokens: int = 1500

    def __post_init__(self):
        if not self.name:
            raise ValueError("PromptSpec.name is required.")
        if self.version < 1:
            raise ValueError(f"{self.name}: version must be >= 1, got {self.version!r}.")
        if not self.system.strip():
            raise ValueError(f"{self.name}: system prompt is empty.")
        if self.tier not in TIERS:
            raise ValueError(
                f"{self.name}: unknown tier {self.tier!r}. Known: {list(TIERS)}. "
                "An unknown tier would route to the CHEAP model without raising."
            )
        if self.max_tokens < 1:
            raise ValueError(f"{self.name}: max_tokens must be >= 1, got {self.max_tokens!r}.")

    def render(self, **kwargs) -> str:
        """Build the content block without invoking a model.

        The seam tests and prompt-review tooling hang off: it makes the exact
        text sent to the model assertable without spending a call.
        """
        return self.build_content(**kwargs)

    def invoke(self, usage=None, **kwargs) -> Any:
        """Render the content and invoke the model, returning parsed JSON.

        Thin by design — ``invoke_json`` already salvages dirty/truncated output,
        and re-implementing any of that here would put two behaviours in the
        codebase where callers expect one.

        Args:
            usage: Optional UsageAccumulator, forwarded to ``invoke_json``.
            **kwargs: Passed to ``build_content``.

        Returns:
            Parsed JSON (dict or list), per ``invoke_json``.
        """
        content = self.render(**kwargs)
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
            self.system,
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
