"""Thin call wrapper around a PromptSpec.

``invoke_prompt`` renders a spec, calls the model through the existing ``llm.invoke``
choke point (so all provider/tier routing is unchanged), validates the parsed JSON against
the spec's schema, and returns the result plus a small metadata envelope carrying the
prompt key + version for traceability.

This lives in the prompts package rather than in ``llm/invoke.py`` so the production
transport module is left untouched. The Phase-1 migration folds this into ``llm/invoke.py``
and rewires the enrichers to call it; for now it is additive and opt-in.
"""

from __future__ import annotations

from typing import Any

from llm.prompts.spec import PromptSpec, validate_output


def invoke_prompt(
    spec: PromptSpec,
    *,
    usage: Any = None,
    strict: bool = False,
    **values: Any,
) -> dict:
    """Render ``spec``, call the model, parse + validate JSON, return result + meta.

    Args:
        spec: the PromptSpec to run.
        usage: optional UsageAccumulator passed through to the transport.
        strict: when True, raise ValueError if the output violates the schema; when False
            (default) the violations are returned under ``meta.schema_errors`` so callers
            can decide.
        **values: variables substituted into the spec's user template.

    Returns:
        ``{"output": <parsed dict>, "meta": {"prompt_key", "prompt_version",
        "schema_errors": [...]}}``.
    """
    # Imported lazily: this keeps ``llm.prompts`` import-safe without Django/transport.
    from llm.invoke import invoke_json

    content = spec.render_user(**values)
    output = invoke_json(
        system=spec.system,
        content=content,
        max_tokens=spec.max_tokens,
        tier=spec.tier,
        usage=usage,
    )

    errors = validate_output(output, spec.output_schema)
    if errors and strict:
        raise ValueError(f"{spec.key} output failed schema: {errors}")

    return {
        "output": output,
        "meta": {
            "prompt_key": spec.key,
            "prompt_version": spec.version,
            "schema_errors": errors,
        },
    }
