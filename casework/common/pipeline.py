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
#
# INTENTIONAL DEVIATION FROM THE DONOR: the donor (0321a85) gates its press
# release stages on a single source type, `CIAA_PRESS_RELEASE`. PRESS_TYPES
# here is deliberately wider -- it also accepts `press_release` and
# `charge_sheet`. This is not a mistake to "correct" back to the donor: Task
# 8 measured MARKDOWN coverage per material type and found `charge_sheet` at
# 100% vs. `press_release` at 8.6%, so narrowing this back to the donor's
# single type would materially shrink how many cases ever clear this
# prerequisite. Keep it wide.
PRESS_TYPES = ("press_release", "ciaa_press_release", "charge_sheet")
COURT_TYPES = ("court_order",)

#: What counts as a REAL description, as opposed to a stub. Donor-verbatim from
#: `enrich_description`'s `_has_substantial_description`.
#:
#: Shared, not duplicated, because two stages must agree on the answer.
#: `enrich_description` uses it to decide a case is already done;
#: `enrich_card` uses it to refuse to write a card grounded in nothing. When
#: only the first held the number, `unmet_prerequisites`' emptiness test was
#: the card's whole defence -- and it passes a whitespace-only description.
SUBSTANTIAL_DESCRIPTION_CHARS = 600


@dataclass(frozen=True)
class Stage:
    """One pipeline stage.

    `provides` names the case field(s) this stage fills in once it succeeds
    (used by future idempotency/"already enriched" checks in Tasks 12-14,
    not by this module). `requires_materials` are material types this stage
    reads source text from -- if non-empty, `unmet_prerequisites` checks
    that at least one bound material of those types has a MARKDOWN link.
    `requires_fields` are case fields that must already be populated (e.g.
    `tags` needs `bigo` filled in first). `requires_entity_roles` narrows that
    for `entities`: a non-empty list is not enough when a stage needs a
    PARTICULAR role -- production `entities` carry `accused, related, location,
    respondent, petitioner, alleged`, so a case holding only `location` binds
    satisfies "entities is non-empty" while giving a name-driven stage nothing to
    work with. `requires_stages` is the DAG edge set consumed by `order_stages`.
    """
    name: str
    provides: Tuple[str, ...] = ()
    requires_fields: Tuple[str, ...] = ()
    requires_entity_roles: Tuple[str, ...] = ()
    requires_materials: Tuple[str, ...] = ()
    requires_stages: Tuple[str, ...] = ()
    run: Optional[Callable] = None


STAGES = {
    # `convert` turns bound RAW/ALTERNATE/SOURCE_PAGE material into a
    # MARKDOWN-role link (Task 12). It has no material/field prerequisites
    # of its own -- it IS the thing every other stage's prerequisite check
    # is waiting on.
    "convert": Stage("convert", provides=("MARKDOWN",)),
    # bigo is PRESS-ONLY on purpose -- do NOT widen this to COURT_TYPES the way
    # `timeline`/`entities` do. That was tried and measured on the 238 bound
    # FY078/079 cases (2026-08-03) and reverted:
    #   - Court orders averaged 52k chars sent vs 2.4k for press releases -- 22x
    #     the input on EVERY case, ~2 min/case vs ~15s, projecting ~8h and >$100
    #     for a run that costs $23 press-only.
    #   - It only changed the answer on the multi-defendant subset (roughly 18 of
    #     238), where the press release states per-defendant figures and no total.
    #   - `bigo` is the ALLEGED loss. The press release IS the charge-stage claim;
    #     the judgment records what was ESTABLISHED, which can be reduced or
    #     overturned. So for this field the judgment is the wrong primary source,
    #     not merely an expensive one.
    # Press-only measured 235/238 acceptable. If the multi-defendant subset ever
    # needs a real total, bind the charge sheet (small, and the document the field
    # is defined against) rather than reading the judgment on all of them.
    "bigo": Stage(
        "bigo", provides=("bigo",),
        requires_materials=PRESS_TYPES,
        requires_stages=("convert",),
    ),
    # `tags` reads no material at all (donor `enrich_tags.py` classifies
    # purely from case fields -- title/allegations/court_cases/description;
    # its only "evidence" occurrence is the literal tag string "evidence
    # tamper"). `bigo` is kept as an ORDERING preference only
    # (`requires_stages`), not a hard gate: the donor's `_detect_amount_tier`
    # returns None (and the amount-tier tag is simply omitted) when `bigo`
    # is None, and the LLM prompt builder guards `if bigo is not None` --
    # the donor tags cases fine with an unknown disputed amount, so a
    # `requires_fields`/`requires_materials` gate here would skip cases the
    # donor does not skip.
    "tags": Stage(
        # NOT ("convert", "bigo"). tags reads no material, so it has no
        # direct dependency on convert; convert is already implied
        # transitively via bigo, whose own requires_stages carries it.
        # Declaring it directly invites a reader to conclude tags needs
        # converted markdown, which is false.
        "tags", provides=("tags",),
        requires_stages=("bigo",),
    ),
    "timeline": Stage(
        "timeline", provides=("timeline",),
        requires_materials=PRESS_TYPES + COURT_TYPES,
        requires_stages=("convert",),
    ),
    # ("key_allegations",) ONLY -- this stage reads the press release, never the
    # verdict, so it cannot answer `missing_details`. `provides` feeds the
    # "already enriched, skip it" checks: a phantom entry would make a case look
    # complete that this stage never touched.
    "allegations": Stage(
        "allegations", provides=("key_allegations",),
        requires_materials=PRESS_TYPES,
        requires_stages=("convert",),
    ),
    # `entities` runs on press release OR court order content -- either
    # alone is sufficient (donor `enrich_related_entities.py::
    # _get_content_for_case` collects both independently; the caller only
    # skips when BOTH are absent: "No press release or court order content
    # -- skipping"). Gating on PRESS_TYPES alone would strand every
    # court-order-only case.
    "entities": Stage(
        "entities", provides=("entities",),
        requires_materials=PRESS_TYPES + COURT_TYPES,
        requires_stages=("convert",),
    ),
    # Gates on both material families, like `timeline`/`entities`: a
    # court-order-only case is still describable.
    #
    # `missing_details` is the one CONDITIONAL entry in this table. The stage
    # writes it from an extra key in the same generate call, but that field
    # needs the COURT ORDER specifically, while this stage runs on either
    # family -- so a press-only case gets a description and no missing_details.
    # Read the tuple as "can provide": an idempotency check requiring BOTH
    # fields would loop forever on those cases.
    #
    # NOT ("description", "title"). The donor regenerated `Case.title` here;
    # `title` now has exactly one owner, `enrich_card`.
    "description": Stage(
        "description", provides=("description", "missing_details"),
        requires_materials=PRESS_TYPES + COURT_TYPES,
        requires_stages=("convert",),
    ),
    # `card` writes the two listing-card fields. It reads NO material -- both
    # fields derive from the `description` already on the case, exactly like
    # `tags` derives from case fields -- so `requires_materials` is empty and
    # gating it on a converted document would strand every case whose
    # description came from somewhere else.
    #
    # Unlike `tags`, though, `description` IS a hard gate here, not just an
    # ordering preference: a card built from an empty description is a card
    # built from nothing. Hence `requires_fields` AND `requires_stages` --
    # `requires_stages` alone only orders, it never checks.
    "card": Stage(
        "card", provides=("title", "short_description"),
        requires_fields=("description",),
        requires_stages=("description",),
    ),
    # `news` searches the open web for independent coverage, LLM-verifies each
    # candidate is about THIS case, and binds the survivors as `evidence`.
    #
    # `requires_fields` is the whole search query, and every one of the three is
    # a hard gate rather than an ordering preference:
    #   - `title` because the query is built from the accused name, the location
    #     and the organisation, all of which `news_search.build_queries` reads
    #     off the title. 2,666 of 2,918 DRAFT titles are the importer's template
    #     ("CIAA Special Court Case 076-CR-0182: बिनोद कुमार भूजेल समेत ५"),
    #     which carries no विरुद्ध clause -- so `accused_names`' title fallback
    #     returns the template string ITSELF as the "name" and every query is
    #     garbage. That is why `card`, the sole owner of `title`, is a
    #     `requires_stages` edge: this stage is worthless before it.
    #   - `entities` because the accused NAMES are the query. The title fallback
    #     exists for a real "X विरुद्ध Y" title, not for a template stub.
    #     `requires_entity_roles=("accused",)` is what actually enforces that:
    #     "entities is non-empty" passed a case carrying only `location` or
    #     `related` binds, which then hit the template-title fallback and spent 12
    #     searches plus a premium batch on garbage queries. The roles in
    #     production are accused/related/location/respondent/petitioner/alleged.
    #   - `court_cases` because the case number is what lets the verifier answer
    #     "high" instead of "medium" (see `news_search.VERIFY_SYSTEM_PROMPT`),
    #     and `high` is the bind bar.
    #
    # `requires_materials` is deliberately EMPTY. The press release is passed to
    # the verifier as context when a converted one exists and its absence is
    # recorded, exactly as the donor did ("No official press release text
    # available", donor:997) -- gating on it would strand the single-document
    # cases the brief specifically requires this stage to be sampled on.
    #
    # `provides` is ("evidence",) -- the same whole-list path `bind` writes.
    "news": Stage(
        "news", provides=("evidence",),
        requires_fields=("title", "entities", "court_cases"),
        requires_entity_roles=("accused",),
        requires_stages=("card", "entities"),
    ),
    # `court_record` reads the case's NGM court record over HTTP -- not a bound
    # document -- so unlike `entities` it needs no material and no `convert`
    # pass. That independence is the point: the accused path used to live inside
    # `enrich_related_entities`, where five gates it had no use for (the
    # already-enriched skip, the MARKDOWN-role prerequisite, the no-source gate,
    # the empty-prompt gate, and any LLM failure) cost a case all its defendants
    # whenever its press release lacked a MARKDOWN role.
    "court_record": Stage(
        "court_record",
        provides=("case_start_date", "case_end_date", "entities"),
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
        if mats and any(markdown_link(m) for m in mats):
            # SATISFIED. Return no material reason at all -- not even the
            # unresolved-entry note below. A case can carry an unresolved
            # entry of some OTHER type alongside a perfectly good converted
            # material of the type this stage needs; reporting that as unmet
            # would gate a stage that is genuinely ready. That over-gating
            # regression shipped once already (a satisfied `bigo` reported
            # unmet because of one unrelated `material: null` entry) and is
            # pinned by test_satisfied_stage_ignores_an_unrelated_unresolved_entry.
            pass
        else:
            # An evidence entry whose `material` is null means the payload
            # came from the case LIST endpoint, which never resolves
            # materials (only the DETAIL endpoint does -- see materials.py).
            # materials_of_type() silently drops those entries, so without
            # this check a case fetched from the wrong endpoint is
            # indistinguishable from a case with genuinely zero bound
            # material: both would fall through to "no bound material of
            # type X", collapsing "can't tell yet" into "definitely can't".
            # Worded consistently with materials.source_text's own message.
            evidence = case.get("evidence") or []
            unresolved = sum(1 for e in evidence if not (e.get("material") or {}))
            if unresolved:
                unmet.append(
                    f"{unresolved} evidence entries with an UNRESOLVED material -- the "
                    "list endpoint returns material:null; use the case DETAIL endpoint"
                )
            if not mats:
                if not unresolved:
                    unmet.append(
                        f"no bound material of type {'/'.join(stage.requires_materials)}")
            else:
                unmet.append(
                    f"no MARKDOWN role on {'/'.join(stage.requires_materials)} "
                    f"({len(mats)} bound, all unconverted)")
    for f in stage.requires_fields:
        if case.get(f) in (None, "", [], {}):
            unmet.append(f"required field {f} is empty")
    # Only when there ARE entities. An empty list is already reported by the
    # `requires_fields` loop above, and adding "no entity with role accused
    # (has: none)" next to "required field entities is empty" says the same thing
    # twice and double-counts the case in the unmet totals.
    entities = case.get("entities") or []
    if stage.requires_entity_roles and entities:
        roles = {(e.get("type") or "").lower()
                 for e in entities if isinstance(e, dict)}
        wanted = {r.lower() for r in stage.requires_entity_roles}
        if not (roles & wanted):
            unmet.append(
                f"no entity with role {'/'.join(sorted(wanted))} "
                f"(has: {', '.join(sorted(roles)) or 'none'})")
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
