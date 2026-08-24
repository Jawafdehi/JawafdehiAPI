"""Validate and apply what the tagger produced. **The only enforcement there is.**

Read that literally. There is no review queue for tag generation: the tagger applies its
own output. So the system prompt in ``prompt_templates/`` *asks* for the rules and this
module *refuses* — and the gap between those two verbs is the entire safety margin.

Everything here corresponds to a rule the prompt states, and each is here because a prompt
cannot be relied on for it:

* ids must be lowercase-kebab ASCII — a model that writes ``Witness Tampering`` once will
  write it again, and it would sit beside ``witness-tampering`` as a second term;
* a case number or an amount is never a tag — these are the two largest classes of junk in
  the corpus today (21 amount tags, all unique by construction), and a model reading a case
  that quotes its own charge number is exactly the situation that produces one;
* the §9 denylist stays banned — ``Corruption`` on 49 of 82 cases is the most *plausible*
  tag a model could propose and the least useful one;
* per-axis counts come from ``TagAxis``, so they are data rather than a constant here;
* the tagger writes three axes and no others — ``status`` and ``verdict`` are court-derived
  (design.md §3 forbids inferring convictions from text) and ``institution``/``person``/
  ``geography`` come from the entities relation (policy §8.6);
* **every tag carries a span quoted from the case text.** This is the strongest guard and
  the cheapest: a substring test. It catches the failure that actually matters, which is a
  confident tag with no basis in the case.

A rejected tag is dropped and reported, never coerced. Partial output is the normal result
and a good one: three valid tags plus one refusal beats a tantrum, and beats four tags
where one is invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import structlog
from django.db import transaction
from jawafdehi_shared.tags.normalize import normalize_tag

from case_tags.cleanup import AMOUNT_RE, BANNED_TERMS, CASE_NUMBER_RE
from case_tags.models import AxisMembers, Tag, TagAxis, TagStatus

logger = structlog.get_logger(__name__)

#: The only axes the tagger may write. Everything else is derived from court data or from
#: the case's entities, and a model writing them would be inventing legal findings or
#: duplicating a resolved relation.
TAGGER_AXES: frozenset[str] = frozenset({"offence", "sector", "governance_level"})

#: A canonical id: lowercase ASCII words joined by single hyphens.
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass
class TagWriteResult:
    """What was applied and, more usefully, what was refused and why."""

    applied: dict[str, list[str]] = field(default_factory=dict)  # axis -> tag ids
    created_terms: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)  # (what, why)

    @property
    def applied_ids(self) -> list[str]:
        return [tag_id for ids in self.applied.values() for tag_id in ids]


def _reject_id(candidate: str) -> str | None:
    """Why ``candidate`` may not be a tag id, or ``None`` if it may.

    Checks the RAW candidate against the junk patterns rather than the normalized one,
    because the two spellings differ by more than case: the corpus writes ``081-CR-0098``
    while a model reading case text is likelier to emit ``081 CR 0098``, and an amount
    asked for as a kebab slug arrives as ``1-crore-25-lakh``. The patterns in
    :mod:`case_tags.cleanup` accept both shapes — they did not until a test here caught
    it.
    """
    if not isinstance(candidate, str) or not candidate.strip():
        return "empty"
    if CASE_NUMBER_RE.search(candidate):
        return "looks like a court case number"
    if AMOUNT_RE.search(candidate):
        return "looks like a bigo amount"
    if normalize_tag(candidate) in BANNED_TERMS:
        return "banned by policy §9"
    if not _SLUG.match(candidate):
        return "not a lowercase-kebab ASCII slug"
    return None


def _span_is_grounded(span: str, case_text: str) -> bool:
    """Is ``span`` actually in the case?

    Whitespace-insensitive, because a model reflowing a quoted line is not a fabrication
    and failing it would train us to ignore this check. Anything beyond that — a
    paraphrase, a plausible-sounding invention — fails, which is the point.
    """
    if not isinstance(span, str) or len(span.strip()) < 8:
        return False
    squash = re.compile(r"\s+")
    return squash.sub(" ", span.strip()) in squash.sub(" ", case_text)


def case_text_for_grounding(case: object) -> str:
    """The text a span may be quoted from: human-authored fields only.

    Not the title (too short to ground anything) and never OCR or scraped text —
    design.md §6.2 ranks raw OCR last for good reason, and a span "found" in garbled OCR
    is not evidence of anything.
    """
    parts: list[str] = []
    for attr in ("description", "short_description"):
        value = getattr(case, attr, None)
        if isinstance(value, str):
            parts.append(value)
    allegations = getattr(case, "key_allegations", None) or []
    parts.extend(a for a in allegations if isinstance(a, str))
    return "\n".join(parts)


@transaction.atomic
def apply_tagger_output(case: object, output: dict, *, detected_by: str) -> TagWriteResult:
    """Validate ``output`` against the vocabulary and write what survives.

    ``output`` is the tagger's JSON: per-axis lists of ``{id, span}`` plus an optional
    ``new_terms``. Nothing about it is trusted.
    """
    result = TagWriteResult()
    grounding = case_text_for_grounding(case)
    axes = {a.id: a for a in TagAxis.objects.all()}
    known = dict(Tag.objects.values_list("id", "axis_id"))

    # New terms first: a tag in the same response may reference one, and refusing the
    # term must then also refuse the tag rather than leaving a dangling id.
    for spec in output.get("new_terms") or []:
        created = _create_term(spec, axes, known, grounding, result)
        if created:
            known[created.id] = created.axis_id
            result.created_terms.append(created.id)

    for axis_id in sorted(TAGGER_AXES):
        axis = axes.get(axis_id)
        if axis is None:
            result.rejected.append((axis_id, "axis does not exist"))
            continue
        accepted: list[str] = []
        for item in output.get(axis_id) or []:
            raw_id = (item or {}).get("id")
            span = (item or {}).get("span") or ""

            reason = _reject_id(raw_id if isinstance(raw_id, str) else "")
            if reason:
                result.rejected.append((str(raw_id), reason))
                continue
            # `_reject_id` returning None guarantees a non-empty str, but the checker
            # cannot see that through the Any coming out of the JSON payload.
            tag_id = str(raw_id)
            if tag_id not in known:
                result.rejected.append((tag_id, "not in the vocabulary"))
                continue
            if known[tag_id] != axis_id:
                result.rejected.append(
                    (tag_id, f"belongs to axis {known[tag_id]!r}, not {axis_id!r}")
                )
                continue
            if not _span_is_grounded(span, grounding):
                result.rejected.append((tag_id, "span is not quoted from the case text"))
                continue
            if tag_id in accepted:
                continue  # duplicate within one axis is a no-op, not an error
            if len(accepted) >= axis.max_per_case:
                result.rejected.append(
                    (tag_id, f"axis {axis_id} allows at most {axis.max_per_case}")
                )
                continue
            accepted.append(tag_id)
        if accepted:
            result.applied[axis_id] = accepted

    _write_to_case(case, result)
    return result


def _create_term(
    spec: dict, axes: dict, known: dict, grounding: str, result: TagWriteResult
) -> Tag | None:
    """Create one new term, or refuse it and say why."""
    slug = (spec or {}).get("id") or ""
    axis_id = (spec or {}).get("axis") or ""
    label_en = (spec or {}).get("label_en") or ""

    reason = _reject_id(slug)
    if reason:
        result.rejected.append((f"new_term {slug!r}", reason))
        return None
    if axis_id not in TAGGER_AXES:
        result.rejected.append((f"new_term {slug!r}", f"may not create on axis {axis_id!r}"))
        return None
    axis = axes.get(axis_id)
    if axis is None or axis.members != AxisMembers.ENUMERATED:
        result.rejected.append((f"new_term {slug!r}", f"axis {axis_id!r} is not enumerated"))
        return None
    if slug in known:
        return Tag.objects.filter(pk=slug).first()  # already exists; treat as a pick
    if not label_en.strip():
        result.rejected.append((f"new_term {slug!r}", "label_en is required"))
        return None
    if not _span_is_grounded((spec or {}).get("span") or "", grounding):
        result.rejected.append((f"new_term {slug!r}", "span is not quoted from the case text"))
        return None

    return Tag.objects.create(
        id=slug,
        axis=axis,
        label_ne=(spec.get("label_ne") or "").strip() or None,
        label_en=label_en.strip(),
        # ACTIVE, not PROPOSED: the scope decision is that the tagger's output is applied,
        # and a term nothing can filter on is not "added". The rationale is kept on the
        # row so a later reviewer can see why it was minted without reading job logs.
        status=TagStatus.ACTIVE,
        note=(spec.get("rationale") or "").strip()[:500],
    )


def _write_to_case(case: object, result: TagWriteResult) -> None:
    """Replace the case's tags with what survived, preserving unresolved values.

    A value already on the case that this vocabulary does not know — geography, an
    office, a person — is KEPT. Those axes are fed from the entities relation, and the
    tagger has no mandate to delete what it was never asked to produce.
    """
    from case_tags.resolve import TagResolver  # noqa: PLC0415

    resolver = TagResolver()
    existing = [t for t in (getattr(case, "tags", None) or []) if isinstance(t, str)]
    unresolved = [t for t in existing if resolver.resolve(t) is None]

    merged: list[str] = []
    for value in result.applied_ids + unresolved:
        if value not in merged:
            merged.append(value)

    if merged != existing:
        case.tags = merged  # type: ignore[attr-defined]
        case.save(update_fields=["tags"])  # type: ignore[attr-defined]
        # Scalar writes bypass post_save, so the live search-index signal never fires.
        # Mirror what case_proposals.apply does and re-index explicitly.
        _schedule_reindex(case)


def _schedule_reindex(case: object) -> None:
    """Re-index after commit. Mirrors ``case_proposals.apply._schedule_reindex``.

    A scalar write bypasses ``post_save``, so the ``jawafdehi_case_search_index`` signal
    never fires and a re-tagged published case would sit stale in search. Best-effort by
    design: an indexing failure must never fail the tagging run, because the tags ARE
    written and the index is a derived read model that reconciliation can repair.
    """

    def _run() -> None:
        try:
            from cases.search_index import index  # noqa: PLC0415

            index(case)
        except Exception:  # noqa: BLE001 - search is best-effort, never fatal
            logger.warning("case_tags.reindex_failed", case_pk=getattr(case, "pk", None))

    transaction.on_commit(_run)


def tagger_vocabulary() -> list[dict]:
    """The enum handed to the model: only the axes it may write, only usable terms.

    Read from the database at call time rather than baked into the prompt, so a term the
    tagger created on an earlier case is available to be REUSED on the next one instead of
    being invented a second time under a different slug. That reuse is the whole reason
    the vocabulary lives in a table.

    A list rather than a dict because the content template iterates it, and Django's
    template language cannot look a dict up by a loop variable without a custom filter.

    Per-term case counts are deliberately absent. They would tell the model which terms
    are established, which is genuinely useful — but computing them costs a pass over the
    whole corpus, and the tagger runs once per case. "Reuse before you create" carries the
    same weight for a fraction of the cost.
    """
    axes = {
        a.id: {
            "id": a.id,
            "label_en": a.label_en,
            "label_ne": a.label_ne,
            "max_per_case": a.max_per_case,
            "terms": [],
        }
        for a in TagAxis.objects.filter(id__in=TAGGER_AXES).order_by("sort_order")
    }
    qs = Tag.objects.filter(axis_id__in=TAGGER_AXES, status=TagStatus.ACTIVE).order_by("id")
    for tag in qs:
        axes[tag.axis_id]["terms"].append(
            {"id": tag.id, "label_en": tag.label_en, "label_ne": tag.label_ne or ""}
        )
    return list(axes.values())
