"""Score an arm's enrichment output with the case reviewer.

WHY THE REVIEWER AND NOT MY OWN JUDGEMENT

`key_allegations` is a list of prose strings and `timeline` entries carry
prose titles/descriptions. Two LLM runs will essentially never produce
byte-identical prose, so exact comparison says nothing about quality. The
project's own benchmark for case quality is the reviewer (`review/`), so that
is what scores these -- not an ad-hoc similarity metric invented here.

HOW IT IS RUN

`review.scorer.score_case` is a PURE FUNCTION of a case dict -- it never
touches the database (rules live in code, `review/rule_defaults.py`, not in a
table). It is called here with:

  * `converted_sources=[]`  -- no source conversion, no downloads
  * `source_analyses=[]`    -- suppresses `judge.analyze_sources`, which
                              would otherwise fire an LLM call
  * deterministic rules only -- the 8 LLM-graded rules are filtered out

so scoring is fully offline, reproducible and zero-variance (deterministic
rules report `confidence="high"`, `variance=0.0`). That matters for an A/B:
LLM-graded rules would add sampling noise on top of the difference under test.

WHAT THE REVIEWER DOES AND DOES NOT DISCRIMINATE -- READ THIS BEFORE
QUOTING ANY SCORE

  * `bigo`            -- well covered. Dedicated GATE rule
                         `bigo_amount_present` (weight 1.2).
  * `timeline`        -- well covered. Dedicated rule `timeline_completeness`
                         scoring count, BS/AD dates, description depth and
                         chronological order.
  * `key_allegations` -- WEAK. No dedicated rule; contributes only a 24-point
                         sub-term inside `structural_completeness`, and that
                         sub-term is a COUNT (`min(n/4, 1) * 24`), not a
                         quality judgement.
  * `tags`            -- WEAKEST. No dedicated rule; an 8-point count sub-term
                         (`min(n/3, 1) * 8`) inside `structural_completeness`.

So a reviewer score difference on tags or allegations reflects HOW MANY items
an arm produced, not how good they are. Presenting it as a quality benchmark
for those two fields would overstate what the reviewer measures. This module
therefore returns the per-rule breakdown, not just the overall number, so the
report can say which rule moved.

CASE TYPE MUST BE HELD CONSTANT

`review.casetype.detect` picks WHICH rules apply, from `court_cases`,
`case_type` and evidence material types. Only the four enriched fields are
spliced per arm; everything else is shared, so both arms are graded against
the identical rule set. `assert_same_rule_basis` enforces this rather than
trusting it.
"""

FIELDS = ("bigo", "tags", "timeline", "key_allegations")

# Rules whose score is actually moved by the fields under test.
RELEVANT_RULES = (
    "bigo_amount_present",
    "timeline_completeness",
    "structural_completeness",
)


class _Config:
    """Stand-in for `review.models.ReviewConfig` (whose getter hits the DB)."""

    def __init__(self, pass_threshold=80, revise_threshold=40, llm_samples=1):
        self.pass_threshold = pass_threshold
        self.revise_threshold = revise_threshold
        self.llm_samples = llm_samples


def deterministic_rules():
    """The reviewer's code-defined rules, minus every LLM-graded one."""
    from review import code_rules

    return [
        r for r in code_rules.get_enabled_rules()
        if r.kind == code_rules.KIND_DETERMINISTIC
    ]


def splice(case, values):
    """Return a copy of `case` with the enriched fields replaced.

    Only the four fields under test are touched; `court_cases`, `case_type`,
    `entities` and evidence are left alone so `casetype.detect` resolves the
    same rule set for every arm.
    """
    out = dict(case)
    for field in FIELDS:
        if field in values:
            out[field] = values[field]
    return out


def assert_same_rule_basis(case_a, case_b):
    """Fail loudly if the two spliced cases would be graded differently.

    If the arms were graded against different rule sets their scores would
    not be comparable, and the difference would look like a quality gap when
    it was really a rule-set gap.
    """
    from review import casetype

    ta = casetype.detect(case_a)
    tb = casetype.detect(case_b)
    if ta != tb:
        raise AssertionError(
            f"case type differs between arms ({ta} vs {tb}); scores would be "
            "graded against different rule sets and are not comparable")
    return ta


def score(case, values, config=None):
    """Score one arm's output for one case. Offline, deterministic.

    Returns the overall score, disposition, and the per-rule breakdown for
    the rules the enriched fields can actually move.
    """
    from review import scorer

    spliced = splice(case, values)
    result = scorer.score_case(
        spliced, [], deterministic_rules(), config or _Config(),
        source_analyses=[])
    rules = {
        r.get("key"): {
            "score": r.get("score"),
            "gate_failed": r.get("gate_failed"),
            "issues": r.get("issues") or [],
        }
        for r in result.get("rules") or []
    }
    return {
        "overall_score": result.get("overall_score"),
        "disposition": result.get("disposition"),
        "gates_pass": result.get("gates_pass"),
        "rules": {k: v for k, v in rules.items() if k in RELEVANT_RULES},
        "all_rules": rules,
    }


def score_arms(case, arm_values, config=None):
    """Score every arm's output for one case against the same rule basis.

    `arm_values` maps arm name -> {field: value}. Returns arm -> score dict,
    plus the shared case type actually used.
    """
    cfg = config or _Config()
    spliced = {arm: splice(case, vals) for arm, vals in arm_values.items()}
    names = sorted(spliced)
    for other in names[1:]:
        assert_same_rule_basis(spliced[names[0]], spliced[other])
    out = {arm: score(case, vals, cfg) for arm, vals in arm_values.items()}
    return out
