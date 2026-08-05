"""Apply an approved proposal's intent onto a Case via the sanctioned write path.

Mirrors ``cases.api_views.CaseViewSet.partial_update``: scalar writes go through
``Case.objects.filter(pk=...).update(...)`` (auto-audited by the audited manager),
the timeline is rebuilt as a plain list, and materials link through
``CaseMaterialReference``. Nothing here bypasses field validation — timeline
entries are validated by ``TimelineItemSerializer`` and raw patches by
``CasePatchSerializer``, exactly like the interactive edit path.
"""

import jsonpatch
import structlog
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.db.models import Max
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from cases.caseworker_serializers import CasePatchSerializer, TimelineItemSerializer
from cases.models import (
    Case,
    CaseMaterialReference,
    RelationshipOutcome,
    RelationshipType,
    validate_material_iri,
)

from .models import SUPPORTED_INTENT_TYPES

logger = structlog.get_logger(__name__)

# Scalar Case fields a raw_patch may touch. Excludes identity/workflow/join
# fields (id, state, case_type, slug, evidence, entities, court_cases): those
# have their own transition/side-effect paths and must not be smuggled in via a
# generic patch.
RAW_PATCH_FIELDS = frozenset(
    {
        "description",
        "short_description",
        "notes",
        "public_notes",
        "tags",
        "key_allegations",
        "missing_details",
        "timeline",
    }
)


def get_case_or_400(slug):
    """Resolve a Case by slug or raise a DRF 400 (the proposal is stale).

    Locks the row (``select_for_update``) — the caller runs this inside the
    approve transaction, and applying an intent read-modify-writes ``timeline``.
    Without the lock two concurrent approvals on the same case can lose an entry
    (a read-modify-write lost update). No-op on sqlite (unit tests)."""
    try:
        return Case.objects.select_for_update().get(slug=slug)
    except Case.DoesNotExist:
        raise ValidationError({"case_slug": f"No case with slug '{slug}'."})


def _schedule_reindex(case):
    """Re-index the case in unified search after commit.

    ``Case.objects.update()`` and join writes bypass ``post_save``, so the
    ``jawafdehi_case_search_index`` signal never fires — exactly why
    ``CaseViewSet.partial_update`` re-indexes explicitly after a PATCH. Mirror it
    so an approved enrichment on a PUBLISHED case doesn't sit stale in search.
    Best-effort: an indexing error must never fail the approval."""

    def _run():
        try:
            from cases.search_index import index

            index(case)
        except Exception:  # noqa: BLE001 - search is best-effort, never fatal
            logger.warning("case_proposal.reindex_failed", case_pk=case.pk)

    transaction.on_commit(_run)


def _schedule_material_visibility(iris):
    """Recompute the given materials' visibility after commit.

    A material's visibility is the MAX over its referring cases' states, so
    linking one to a (published) case must recompute it — otherwise a just-linked
    court order stays PRIVATE until the periodic backstop runs. Mirrors
    ``cases.api_views._recompute_material_visibility``; best-effort + cross-DB."""
    iris = [iri for iri in dict.fromkeys(iris) if iri]
    if not iris:
        return

    def _run():
        try:
            from materials.visibility import recompute_material_visibility

            for iri in iris:
                recompute_material_visibility(iri)
        except Exception:  # noqa: BLE001 - visibility is best-effort, never fatal
            logger.warning("case_proposal.visibility_recompute_failed", count=len(iris))

    transaction.on_commit(_run)


@transaction.atomic
def apply_intent(case, intent):
    """Apply ``intent`` to ``case``. Raises DRF ``ValidationError`` (400) on any
    unsupported type, malformed payload, or validation failure. Atomic: a
    failure leaves the Case untouched."""
    itype = (intent or {}).get("type")
    if itype == "append_timeline_entry":
        result = _append_timeline_entry(case, intent)
    elif itype == "link_material":
        result = _link_material(case, intent)
    elif itype == "raw_patch":
        result = _raw_patch(case, intent)
    elif itype == "set_entity_outcome":
        result = _set_entity_outcome(case, intent)
    else:
        # Every accepted type is applyable — the serializer and this dispatch share
        # one vocabulary, so there is no "staged but uncommittable" middle state.
        raise ValidationError(
            {"intent": f"Unknown intent type '{itype}'. Applyable: {', '.join(SUPPORTED_INTENT_TYPES)}."}
        )
    # Mirror the sanctioned PATCH path: scalar/join writes bypass post_save, so
    # the live search-index signal never runs — re-index explicitly on commit.
    _schedule_reindex(case)
    return result


def _append_timeline_entry(case, intent):
    entry = (intent or {}).get("entry")
    if not isinstance(entry, dict):
        raise ValidationError({"intent": "append_timeline_entry requires an `entry` object."})
    # Validate the entry with the SAME serializer the interactive edit path uses.
    ser = TimelineItemSerializer(data=entry)
    ser.is_valid(raise_exception=True)
    clean = {k: v for k, v in ser.validated_data.items() if v not in (None, "")}
    timeline = list(case.timeline or [])
    timeline.append(clean)
    Case.objects.filter(pk=case.pk).update(timeline=timeline, updated_at=timezone.now())
    return {"field": "timeline", "appended": clean}


def _link_material(case, intent):
    iri = (intent or {}).get("material")
    relation = (intent or {}).get("relation", "") or ""
    if not iri:
        raise ValidationError({"intent": "link_material requires a `material` IRI."})
    try:
        validate_material_iri(iri)
    except DjangoValidationError as exc:
        raise ValidationError({"intent": list(exc.messages)})
    # Append (don't replace) — next ordinal after the case's existing evidence.
    ordinal = (case.material_references.aggregate(m=Max("ordinal"))["m"] or 0) + 1
    _, created = CaseMaterialReference.objects.get_or_create(
        case=case,
        material_iri=iri,
        defaults={
            "ordinal": ordinal,
            "additional_details": f"relation: {relation}" if relation else "",
        },
    )
    if created:
        # A join-only write never touches the Case row, so bump updated_at (the
        # ETag/optimistic-concurrency basis) exactly like the sanctioned
        # relation-only PATCH, and recompute the linked material's visibility.
        Case.objects.filter(pk=case.pk).update(updated_at=timezone.now())
        _schedule_material_visibility([iri])
    return {"linked_material": iri, "created": created}


def _raw_patch(case, intent):
    ops = (intent or {}).get("patch")
    if not isinstance(ops, list) or not ops:
        raise ValidationError({"intent": "raw_patch requires a non-empty `patch` op list."})
    # Reject any op that touches a field outside the allowed scalar set.
    for op in ops:
        for key in ("path", "from"):
            pointer = op.get(key)
            if pointer:
                top = pointer.lstrip("/").split("/", 1)[0]
                if top not in RAW_PATCH_FIELDS:
                    raise ValidationError(
                        {
                            "intent": (
                                f"raw_patch path '{pointer}' is not allowed. "
                                f"Allowed top-level fields: {sorted(RAW_PATCH_FIELDS)}."
                            )
                        }
                    )
    snapshot = {f: getattr(case, f) for f in RAW_PATCH_FIELDS}
    try:
        patched = jsonpatch.apply_patch(snapshot, ops)
    except (jsonpatch.JsonPatchException, jsonpatch.JsonPointerException) as exc:
        raise ValidationError({"intent": f"patch failed: {exc}"})
    # Validate the post-patch values with the same serializer the edit path uses.
    ser = CasePatchSerializer(data=patched, partial=True)
    ser.is_valid(raise_exception=True)
    updates = {f: ser.validated_data[f] for f in RAW_PATCH_FIELDS if f in ser.validated_data}
    if updates:
        Case.objects.filter(pk=case.pk).update(updated_at=timezone.now(), **updates)
    return {"patched_fields": sorted(updates.keys())}


# Terminal outcomes a proposal may set. ``charged`` is excluded on purpose: it is
# the default a relationship already starts at, so "propose charged" is either a
# no-op or a regression from a decided verdict back to undecided — and un-deciding
# a case is not an enrichment, it is a correction that belongs in the admin with a
# human looking at why.
PROPOSABLE_OUTCOMES = frozenset(
    {
        RelationshipOutcome.CONVICTED,
        RelationshipOutcome.ACQUITTED,
        RelationshipOutcome.ABATED,
    }
)


def _set_entity_outcome(case, intent):
    """Set the verdict outcome on this case's ACCUSED entity relationships.

    The one enrichment ``raw_patch`` cannot express, because ``entities`` is not a
    Case scalar: it is a relationship guarded by the ``outcome_only_on_accused``
    CHECK constraint. Resolving every ``nes_id`` against THIS case's own
    relationships first is what makes that safe — a proposal cannot reach an
    entity it was not already bound to, and cannot set an outcome on a
    non-accused role (which the DB would reject with an IntegrityError, i.e. a
    500 where a 400 belongs).

    All-or-nothing by design. A verdict is one fact about every defendant, so a
    partial application would leave the case asserting that some of the acquitted
    are still merely charged — the exact defect this intent exists to remove.
    """
    outcomes = (intent or {}).get("outcomes")
    if not isinstance(outcomes, list) or not outcomes:
        raise ValidationError(
            {"intent": "set_entity_outcome requires a non-empty `outcomes` list."}
        )

    # Only ACCUSED relationships are addressable; anything else is rejected below
    # with the role named, rather than failing the CHECK constraint at write time.
    by_nes_id = {rel.nes_id: rel for rel in case.entity_relationships.all()}
    resolved = []
    for item in outcomes:
        if not isinstance(item, dict):
            raise ValidationError({"intent": "each `outcomes` entry must be an object."})
        nes_id, outcome = item.get("nes_id"), item.get("outcome")
        if not nes_id or not outcome:
            raise ValidationError(
                {"intent": "each `outcomes` entry requires `nes_id` and `outcome`."}
            )
        if outcome not in PROPOSABLE_OUTCOMES:
            raise ValidationError(
                {
                    "intent": (
                        f"outcome '{outcome}' is not proposable. "
                        f"Allowed: {sorted(PROPOSABLE_OUTCOMES)}."
                    )
                }
            )
        rel = by_nes_id.get(nes_id)
        if rel is None:
            raise ValidationError(
                {"intent": f"'{nes_id}' is not an entity of case '{case.slug}'."}
            )
        if rel.relationship_type != RelationshipType.ACCUSED:
            raise ValidationError(
                {
                    "intent": (
                        f"'{nes_id}' is bound as '{rel.relationship_type}', not accused; "
                        "an outcome is meaningful only for the accused role."
                    )
                }
            )
        resolved.append((rel, outcome))

    changed = []
    for rel, outcome in resolved:
        if rel.outcome == outcome:
            continue
        previous = rel.outcome
        rel.outcome = outcome
        # ``save()`` (not ``queryset.update()``) so the model's own normalisation
        # and ``full_clean()`` run, exactly as the admin edit path does. No
        # ``update_fields``: this model has no ``updated_at``, and narrowing the
        # write would skip nothing worth skipping on a single-column change.
        rel.save()
        changed.append({"nes_id": rel.nes_id, "from": previous, "to": outcome})

    if changed:
        # A relationship-only write never touches the Case row, so bump
        # ``updated_at`` — the ETag/optimistic-concurrency basis — exactly like
        # the relation-only PATCH path does.
        Case.objects.filter(pk=case.pk).update(updated_at=timezone.now())
    return {"outcomes_changed": changed, "unchanged": len(resolved) - len(changed)}
