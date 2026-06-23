"""Code-enforced review rules.

The review rule set is defined entirely in code (``rule_defaults.DEFAULT_RULES``)
and is NOT stored in the database — the old editable ``Rule`` table was removed.
Rules are read-only at runtime: the scorer/pipeline consume them and the API
exposes them for display, but nothing edits them. To change a rule (add, remove,
reword, re-weight, toggle), edit ``rule_defaults.py``.

Each ``CodeRule`` exposes exactly the attributes the scorer reads from the old
Rule model instances, so ``scorer.score_case`` needed no changes.
"""

from .rule_defaults import DEFAULT_RULES

KIND_DETERMINISTIC = "deterministic"
KIND_LLM = "llm"


class CodeRule:
    """An immutable, code-defined review rule (drop-in for the old Rule model)."""

    KIND_DETERMINISTIC = KIND_DETERMINISTIC
    KIND_LLM = KIND_LLM

    def __init__(self, index, spec):
        # Stable 1-based id by definition order, so the API/UI can key off it.
        self.id = index + 1
        self.key = spec["key"]
        self.title = spec["title"]
        self.category = spec.get("category", "General")
        self.kind = spec.get("kind", KIND_DETERMINISTIC)
        self.detector = spec.get("detector", "")
        self.description = spec.get("description", "")
        self.good_examples = spec.get("good_examples", "")
        self.bad_examples = spec.get("bad_examples", "")
        self.condition_text = spec.get("condition_text", "")
        self.applies_to = spec.get("applies_to", ["ALL"])
        self.weight = spec.get("weight", 1.0)
        self.is_gate = spec.get("is_gate", False)
        self.gate_min = spec.get("gate_min", 50)
        # Optional model-tier override for LLM rules ("premium"/"cheap"). Empty
        # means "auto": gate rules -> premium, routine rules -> cheap.
        self.tier = spec.get("tier", "")
        self.enabled = spec.get("enabled", True)
        self.order = spec.get("order", 100)

    def __repr__(self):
        return f"<CodeRule {self.key} ({self.category})>"


def get_rules():
    """All code rules, in display order (by ``order``, then definition index)."""
    rules = [CodeRule(i, s) for i, s in enumerate(DEFAULT_RULES)]
    rules.sort(key=lambda r: (r.order, r.id))
    return rules


def get_enabled_rules():
    """Enabled rules only — what the scorer grades against."""
    return [r for r in get_rules() if r.enabled]


def get_rule(rule_id):
    """Return a single rule by its stable id, or ``None``."""
    for r in get_rules():
        if r.id == rule_id:
            return r
    return None


def as_dict(rule):
    """Serialize a CodeRule to the same JSON shape the old RuleSerializer used."""
    return {
        "id": rule.id,
        "key": rule.key,
        "title": rule.title,
        "category": rule.category,
        "description": rule.description,
        "good_examples": rule.good_examples,
        "bad_examples": rule.bad_examples,
        "condition_text": rule.condition_text,
        "applies_to": rule.applies_to,
        "kind": rule.kind,
        "detector": rule.detector,
        "weight": rule.weight,
        "is_gate": rule.is_gate,
        "gate_min": rule.gate_min,
        "tier": rule.tier,
        "enabled": rule.enabled,
        "order": rule.order,
    }
