# casework/common/pipeline.py
"""Stage registry and prerequisite DAG.

The donor management commands (`enrich_missing_bigo`, `enrich_tags`,
`enrich_timeline`, `enrich_allegations`, `enrich_related_entities`) were five
standalone commands with no shared sequencing -- each one independently
re-derived "can I run on this case?" from scratch, and none of them knew
about the others. This module is new design, not a port: it is the single
place that says what depends on what, and the single place that says why a
stage did NOT run on a given case.

The dependency chain starts at the MATERIAL, not at a case field like
`bigo`. A case with evidence bound but no MARKDOWN-role material cannot be
enriched -- that is a prerequisite failure, not an error, and it must
surface as an explicit unmet-prerequisite reason, never as a silent skip
indistinguishable from "already enriched" (see `materials.source_text`,
which this module intentionally mirrors in spirit: report why, don't just
return empty).

Stage names are shared with `casework.common.llm.TIERS` -- keep the two in
lockstep (see `test_stage_names_match_llm_tier_names`); a mismatch there
degrades silently (`tier_for` just returns the default tier) rather than
raising, so nothing else will catch a rename here.
"""
import collections
from dataclasses import dataclass, field
from typing import Callable, Optional, Tuple

from casework.common.materials import markdown_link, materials_of_type

# Material types an enricher can extract source text from. Kept here (not
# imported from an enricher module) because Task 11 has no enricher modules
# yet -- these constants are the contract Tasks 12-14 build against.
PRESS_TYPES = ("press_release", "ciaa_press_release", "charge_sheet")
COURT_TYPES = ("court_order",)


@dataclass(frozen=True)
class Stage:
    """One pipeline stage.

    `provides` names the case field(s) this stage fills in once it succeeds
    (used by future idempotency/"already enriched" checks in Tasks 12-14,
    not by this module). `requires_materials` are material types this stage
    reads source text from -- if non-empty, `unmet_prerequisites` checks
    that at least one bound material of those types has a MARKDOWN link.
    `requires_fields` are case fields that must already be populated (e.g.
    `tags` needs `bigo` filled in first). `requires_stages` is the DAG edge
    set consumed by `order_stages`.
    """
    name: str
    provides: Tuple[str, ...] = ()
    requires_fields: Tuple[str, ...] = ()
    requires_materials: Tuple[str, ...] = ()
    requires_stages: Tuple[str, ...] = ()
    run: Optional[Callable] = None


STAGES = {
    # `convert` turns bound RAW/ALTERNATE/SOURCE_PAGE material into a
    # MARKDOWN-role link (Task 12). It has no material/field prerequisites
    # of its own -- it IS the thing every other stage's prerequisite check
    # is waiting on.
    "convert": Stage("convert", provides=("MARKDOWN",)),
    "bigo": Stage(
        "bigo", provides=("bigo",),
        requires_materials=PRESS_TYPES,
        requires_stages=("convert",),
    ),
    "tags": Stage(
        "tags", provides=("tags",),
        requires_fields=("bigo",),
        requires_materials=PRESS_TYPES,
        requires_stages=("convert", "bigo"),
    ),
    "timeline": Stage(
        "timeline", provides=("timeline",),
        requires_materials=PRESS_TYPES + COURT_TYPES,
        requires_stages=("convert",),
    ),
    "allegations": Stage(
        "allegations", provides=("key_allegations", "missing_details"),
        requires_materials=PRESS_TYPES,
        requires_stages=("convert",),
    ),
    "entities": Stage(
        "entities", provides=("entities",),
        requires_materials=PRESS_TYPES,
        requires_stages=("convert",),
    ),
}


def order_stages(names):
    """Return `names` (deduplicated) in a deterministic topological order.

    Ordering is over `requires_stages` edges restricted to the requested
    set -- a dependency that was NOT asked for is not injected (e.g.
    `order_stages(["bigo"])` alone does not insert "convert"; running
    `bigo` on a case with no converted material is instead caught at
    runtime by `unmet_prerequisites` and reported, per the "never a silent
    skip" rule). Raises `KeyError` for a name not in `STAGES`, and
    `ValueError` if `requires_stages` ever forms a cycle.
    """
    names = list(dict.fromkeys(names))
    for n in names:
        if n not in STAGES:
            raise KeyError(f"unknown stage: {n}")
    wanted, ordered, seen = set(names), [], set()

    def visit(name, trail=()):
        if name in seen:
            return
        if name in trail:
            raise ValueError(f"cycle through {name}")
        for dep in sorted(STAGES[name].requires_stages):
            if dep in wanted:
                visit(dep, trail + (name,))
        seen.add(name)
        ordered.append(name)

    for name in sorted(names):
        visit(name)
    return ordered


def unmet_prerequisites(stage, case):
    """Reasons `stage` cannot run on `case` right now. Empty list == ready.

    Never returns a bare boolean or raises for "not ready" -- every reason
    is a human-readable string so a `RunReport` can show it verbatim,
    mirroring how `materials.source_text` reports unusable material.
    """
    unmet = []
    if stage.requires_materials:
        mats = materials_of_type(case, stage.requires_materials)
        if not mats:
            unmet.append(
                f"no bound material of type {'/'.join(stage.requires_materials)}")
        elif not any(markdown_link(m) for m in mats):
            unmet.append(
                f"no MARKDOWN role on {'/'.join(stage.requires_materials)} "
                f"({len(mats)} bound, all unconverted)")
    for f in stage.requires_fields:
        if case.get(f) in (None, "", [], {}):
            unmet.append(f"required field {f} is empty")
    return unmet


@dataclass
class RunReport:
    """Per-case, per-stage outcomes for one pipeline run.

    `status` is caller-defined (the enrichers use at least "unmet",
    "skipped", "enriched", "error") -- this module doesn't enumerate a
    closed set of statuses because Tasks 12-14 own what counts as each.
    What it DOES guarantee is that "unmet" and "skipped" are counted
    separately: an unmet prerequisite is a case this stage could not
    attempt, a skip is a case it could have attempted but chose not to
    (e.g. already filled in) -- collapsing the two would make an
    unreachable case look identical to an intentionally-skipped one.
    """
    rows: list = field(default_factory=list)

    def record(self, slug, stage, status, reason=""):
        self.rows.append(
            {"slug": slug, "stage": stage, "status": status, "reason": reason})

    def summary(self):
        return dict(collections.Counter(r["status"] for r in self.rows))

    def unmet_reasons(self):
        return collections.Counter(
            r["reason"] for r in self.rows if r["status"] == "unmet")
