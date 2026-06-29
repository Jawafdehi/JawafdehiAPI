"""Central prompt registry.

Prompts live here as ``PromptSpec`` data (see ``spec.py``) instead of inline string
constants scattered across the enrichers. The registry is intentionally lazy: importing
``llm.prompts`` is cheap and Django-free; the per-prompt modules are imported on demand by
``get()`` because they pull the live prompt constants from their enricher (which is fine
under Django, but we don't want to force it at package import).

Each entry maps a stable key to the module that exposes a ``SPEC``. New prompts are added
by writing a module next to ``enrich_missing_bigo.py`` and registering its key here.
"""

from __future__ import annotations

import importlib

from llm.prompts.spec import PromptSpec, validate_output

# key -> dotted module path exposing a module-level ``SPEC``.
REGISTRY: dict[str, str] = {
    "enrich.missing_bigo": "llm.prompts.enrich_missing_bigo",
}


def get(key: str) -> PromptSpec:
    """Return the ``PromptSpec`` registered under ``key``.

    Raises KeyError for an unknown key.
    """
    try:
        module_path = REGISTRY[key]
    except KeyError as exc:
        raise KeyError(
            f"unknown prompt key {key!r}; known keys: {sorted(REGISTRY)}"
        ) from exc
    module = importlib.import_module(module_path)
    return module.SPEC


def all_keys() -> list[str]:
    """List every registered prompt key."""
    return sorted(REGISTRY)


__all__ = ["PromptSpec", "validate_output", "get", "all_keys", "REGISTRY"]
