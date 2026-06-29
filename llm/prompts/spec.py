"""PromptSpec: a prompt as versioned data, not an inline string constant.

This generalises the prompts-as-data pattern already used by ``review/rule_defaults.py``
(rules carry title + description + good/bad examples + weight + gate) to the enricher
prompts, so every prompt has a stable key, an immutable version, a declared variable
surface, an output schema, and golden examples that double as eval seed data.

The dataclass is deliberately dependency-free (stdlib only) so importing the registry
never pulls Django or the LLM transport. Rendering is plain ``str.format`` to mirror how
the enrichers already build their user prompts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class PromptSpec:
    """A single prompt, captured as data.

    Fields mirror what an enricher needs to make one LLM call plus what an eval needs to
    score it. ``output_schema`` is a small JSON-Schema-ish dict (see ``validate_output``);
    ``examples`` is a path to / list of golden reference cases.
    """

    key: str
    version: str
    system: str
    user_template: str
    tier: str = "premium"
    max_tokens: int = 2000
    output_schema: Optional[dict] = None
    examples: str = ""
    source_ref: str = ""
    notes: str = ""
    variables: tuple = field(default_factory=tuple)

    def render_user(self, **values: Any) -> str:
        """Render the user prompt by substituting declared variables."""
        return self.user_template.format(**values)


def validate_output(obj: Any, schema: Optional[dict]) -> list[str]:
    """Validate ``obj`` against a tiny JSON-Schema subset; return a list of errors.

    Supports the handful of constraints the enricher outputs actually use — ``type``,
    ``required``, per-property ``type``/``enum``/``nullable`` — without taking a
    jsonschema dependency. An empty list means the object conforms.
    """
    if not schema:
        return []
    errors: list[str] = []

    expected_type = schema.get("type")
    if expected_type == "object" and not isinstance(obj, dict):
        return [f"expected object, got {type(obj).__name__}"]

    if isinstance(obj, dict):
        for key in schema.get("required", []):
            if key not in obj:
                errors.append(f"missing required field: {key}")

        for prop, rule in schema.get("properties", {}).items():
            if prop not in obj:
                continue
            value = obj[prop]
            if value is None:
                if not rule.get("nullable"):
                    errors.append(f"field {prop} is null but not nullable")
                continue
            ptype = rule.get("type")
            if ptype and not _matches_type(value, ptype):
                errors.append(
                    f"field {prop}: expected {ptype}, got {type(value).__name__}"
                )
            enum = rule.get("enum")
            if enum and value not in enum:
                errors.append(f"field {prop}: {value!r} not in {enum}")

    return errors


_TYPE_MAP = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _matches_type(value: Any, ptype: str) -> bool:
    py = _TYPE_MAP.get(ptype)
    if py is None:
        return True
    # bool is a subclass of int; keep them distinct so a flag never satisfies integer.
    if ptype == "integer" and isinstance(value, bool):
        return False
    return isinstance(value, py)
