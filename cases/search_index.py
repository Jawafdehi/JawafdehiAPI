"""Unified-search indexer for Jawafdehi cases (``jawafdehi-cases`` index).

Projects a ``Case`` into the common index doc. CASE-ONLY-PUBLISHED RULE (plan
§5, decision #6): only ``state == PUBLISHED`` cases enter the index — the search
index is all-public, so there is NO visibility/ACL field. ``index(case)`` upserts
when published and DELETES from the index when the case is not published (so a
case that leaves PUBLISHED is removed). ``should_index(case)`` exposes the rule.

Field mapping:
* ``iri``            ← ``Case.public_iri`` (``https://jawafdehi.org/case/<slug>``,
  minted at publish — the document ``_id``),
* ``type``           ← ``"Case"`` (+ the platform ``case_type`` in keywords),
* ``title_ne/en``    ← ``Case.title`` (script-bucketed; cases store one title),
* ``title_translit`` ← shared transliteration of the title,
* ``body``           ← ``description`` + ``key_allegations`` (joined),
* ``keywords``       ← ``tags`` (+ ``case_type``),
* ``identifiers``    ← the IRI, the slug, and the ``court_cases`` references,
* ``date``           ← ``case_start_date`` (else created date),
* ``case_status``    ← coarse ongoing/closed/others (mirrors the SPA rule); a
  dedicated keyword (NOT the generic ``status``, which NGM uses for its scraper
  enrichment flag) so the unified search can facet/filter cases without collision,
* ``bigo``           ← ``Case.bigo`` (बिगो, whole NPR) promoted to a top-level
  ``long`` so the unified search can RANGE-filter on it; the card copy under
  ``raw`` is return-only (``raw`` is ``enabled: false``, hence unqueryable),
* ``raw``            ← a light record PLUS a ``card`` payload (return-only): every
  field the SPA case list/card renders — ``short_description``, ``key_allegations``,
  ``tags``, dates, ``bigo``, thumbnail/banner, the ``timeline`` (major events), and
  the resolved entity binds — denormalized so a search hit renders WITHOUT a
  second fetch to ``/api/cases/{slug}/``.

Denormalized entity names come from NES at index time, so a case must be
re-indexed when a referenced entity is renamed (a scheduled ``reindex_cases``
reconcile covers this — see the plan's WS3); the write-time signals only fire on
``Case`` itself.

Best-effort: ``index`` / ``delete`` log and swallow an OpenSearch error, because
they run from write-time signals where a search blip must not fail a case save.
``index_now`` / ``delete_now`` are the same functions without the swallow, for
callers that have a retry budget to spend on the failure.
"""

from __future__ import annotations

from typing import Any

from jawafdehi_shared.search.indexing import (
    best_effort,
    delete_doc,
    name_to_titles,
    title_translit,
    upsert_doc,
)
from jawafdehi_shared.search.opensearch import CASE_INDEX, make_client

from .validators import parse_courtcase_ref

SOURCE_APP = "jawafdehi"
TYPE_TOKEN = "Case"


def should_index(case: Any) -> bool:
    """True iff the case belongs in the all-public index (state == PUBLISHED)."""
    # Compare against the value so we don't import CaseState at module load
    # (keeps this importable in pure-shaping contexts); CaseState.PUBLISHED == "PUBLISHED".
    return str(getattr(case, "state", "")) == "PUBLISHED"


def _case_iri(case: Any) -> str | None:
    """The case's public @id IRI (only set when PUBLISHED)."""
    return getattr(case, "public_iri", None)


def _iso(value: Any) -> str | None:
    """ISO-8601 string for a date/datetime value (or ``None``)."""
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _derive_status(case: Any) -> str:
    """Coarse case lifecycle for the ``case_status`` facet, mirroring the SPA rule.

    ``ongoing`` = a start date but no end date; ``closed`` = both dates present;
    ``others`` = neither (or only an end date). Kept in lockstep with the frontend
    ``getCaseStatus`` so the server facet and the client badge agree."""
    has_start = getattr(case, "case_start_date", None) is not None
    has_end = getattr(case, "case_end_date", None) is not None
    if has_start and not has_end:
        return "ongoing"
    if has_start and has_end:
        return "closed"
    return "others"


# Signed-64-bit bounds — the domain of the ``bigo`` field's ``long`` mapping (and
# of the ``BigIntegerField`` it comes from). A value outside them is rejected by
# OpenSearch, which fails the whole document, so ``_bigo`` screens for it.
_LONG_MIN, _LONG_MAX = -(2**63), 2**63 - 1


def _bigo(case: Any) -> int | None:
    """बिगो as a whole-rupee ``int`` for the top-level numeric field (or ``None``).

    The model column is a ``BigIntegerField``, so the live index paths hand this a
    plain ``int``. The coercion is for the attribute-shaped stand-ins that also
    reach ``build_doc`` (test doubles, records rehydrated from API JSON), where an
    amount can arrive as a numeric string — ``enrich_tags._detect_amount_tier``
    copes with the same thing. Like every other field here, the value is read with
    ``getattr``; this indexer shapes objects, not mappings.

    Anything the ``long`` mapping would refuse — non-numeric, a bool, or a
    magnitude past the 64-bit bounds — yields ``None`` so the FIELD is dropped.
    That containment is the whole point: OpenSearch rejects a bad value by
    rejecting the entire document, which would take a published case out of search
    altogether over one unusable number. Losing the amount is recoverable; losing
    the case is not.

    ``0`` is passed through as a real value, not treated as "unrecorded" — no
    published case records a zero बिगो, and inventing that rule here would make
    an honest zero silently unfilterable.
    """
    value = getattr(case, "bigo", None)
    if value is None or isinstance(value, bool):
        return None
    try:
        # ``int`` FIRST, so a large amount keeps full precision: a float
        # round-trip is lossy above 2**53 and can round a perfectly valid figure
        # up over the ``long`` ceiling, dropping the field for no reason.
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        try:
            # A decimal string or float ("1.9") — truncate to whole rupees.
            number = int(float(value))
        except (TypeError, ValueError, OverflowError):
            return None
    return number if _LONG_MIN <= number <= _LONG_MAX else None


def _safe_resolve_entities(case: Any) -> list[dict[str, Any]]:
    """Resolve the case's entity binds to card display details (best-effort).

    Uses the same shaper as ``CaseSerializer.get_entities``
    (``nes_resolver.build_entity_binds``) so the card and the API can't drift.
    Any failure — no relation manager (e.g. a bare test object) or NES unreachable
    — yields ``[]`` (or ``None`` names), so the case is still indexed and a later
    ``reindex_cases`` reconciles the names."""
    try:
        relationships = list(case.entity_relationships.all())
    except (AttributeError, TypeError):
        return []
    if not relationships:
        return []
    from cases.services.nes_resolver import build_entity_binds, resolve_entities

    try:
        resolved = resolve_entities(rel.nes_id for rel in relationships)
    except Exception:  # noqa: BLE001 — best-effort; index without names on failure.
        resolved = {}
    # build_entity_binds is inside the guard too: a corrupt relationship row
    # (missing nes_id/relationship_type) must not crash the best-effort indexer.
    try:
        return build_entity_binds(relationships, resolved)
    except (AttributeError, TypeError):
        return []


def _key_allegations(case: Any) -> list[str]:
    """The case's non-empty allegation strings, stripped."""
    return [
        a.strip()
        for a in (getattr(case, "key_allegations", None) or [])
        if isinstance(a, str) and a.strip()
    ]


def _build_body(case: Any, short: str | None) -> str | None:
    """Free-text recall body: description, then allegations, then short description."""
    body_parts: list[str] = []
    description = getattr(case, "description", None)
    if description and description.strip():
        body_parts.append(description.strip())
    body_parts.extend(_key_allegations(case))
    if short and short.strip():
        body_parts.append(short.strip())
    return "\n".join(body_parts) or None


def _expand_tags(tags: list[str]) -> tuple[list[str], list[str]]:
    """Canonical tag ids -> (ids + broader roll-up, display labels).

    The roll-up is applied at INDEX time, so ``?tags=land`` is a plain term query
    against a field that already contains ``land`` on every land-grab case. Doing it
    at query time instead would mean rewriting each incoming tag into a bool-should
    over its narrower tags, on every request, against a vocabulary the query layer
    would have to load.

    Labels come back separately because they feed ``keywords`` (analyzed text) while
    the ids feed ``tags`` (exact keyword). Both scripts are emitted: someone typing
    "भूमि प्रशासन" and someone typing "land administration" must both hit the case.

    Unknown ids pass through unchanged in BOTH lists. A tag can only be unknown here
    if the vocabulary lost a term the cases still carry, and silently dropping it
    would make that invisible; keeping it means the case stays findable by the one
    string it has.

    One query, not one per tag: reindexing walks every case, and a per-tag lookup
    would be an N+1 across the whole corpus.
    """
    if not tags:
        return [], []

    from case_tags.models import Tag

    rows = {
        t.id: t
        for t in Tag.objects.filter(pk__in=tags).select_related("broader")
    }

    expanded: list[str] = []
    labels: list[str] = []

    def add(tag_id: str) -> None:
        if tag_id not in expanded:
            expanded.append(tag_id)

    for tag_id in tags:
        add(tag_id)
        row = rows.get(tag_id)
        if row is None:
            # Not in the vocabulary — keep the raw string as its own label.
            if tag_id not in labels:
                labels.append(tag_id)
            continue
        for label in (row.label_ne, row.label_en):
            if label and label not in labels:
                labels.append(label)
        # ``broader`` is capped at one level by Tag.clean(), so this is a single hop,
        # not a walk. The parent's labels are NOT added: a land-grab case should not
        # become a text hit for "भूमि प्रशासन" just because both roll up to land.
        if row.broader_id:
            add(row.broader_id)

    return expanded, labels


def _build_identifiers(case: Any, iri: str | None, slug: str | None) -> list[str]:
    """Exact-match identifiers: the IRI, the slug, and every court-case ref.

    Court-case refs are canonical @id IRIs; also carry the bare case number in
    both casings. NB: ``identifiers`` is a plain keyword field that the unified
    free-text query does NOT search — it exists for exact-match consumers, so
    mirror the NGM courtcase docs' verbatim-UPPERCASE number alongside the IRI's
    lowercase one to keep cross-doc lookups consistent.
    """
    identifiers: list[str] = [i for i in (iri, slug) if i]
    for ref in getattr(case, "court_cases", None) or []:
        if not isinstance(ref, str) or not ref:
            continue
        candidates = [ref]
        parsed = parse_courtcase_ref(ref)
        if parsed:
            candidates.append(parsed[1])
            candidates.append(parsed[1].upper())
        for candidate in candidates:
            if candidate not in identifiers:
                identifiers.append(candidate)
    return identifiers


def _apply_dates(doc: dict[str, Any], case: Any) -> None:
    """Set ``date``/``created_at``/``updated_at``, each only when available.

    ``date`` prefers the case start date and falls back to the creation date, so
    a case with no explicit start is still sortable.
    """
    start = getattr(case, "case_start_date", None)
    created = getattr(case, "created_at", None)
    if start is not None:
        doc["date"] = _iso(start)
    elif created is not None and hasattr(created, "date"):
        doc["date"] = created.date().isoformat()
    if created is not None:
        doc["created_at"] = _iso(created)
    updated = getattr(case, "updated_at", None)
    if updated is not None:
        doc["updated_at"] = _iso(updated)


def _build_card(
    case: Any,
    *,
    slug: str | None,
    title: str,
    short: str | None,
    tags: list[str],
    case_type: Any,
    case_status: str,
    entities: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Everything the SPA case card/list renders, denormalized.

    Lives under ``raw`` (mapping ``enabled: false``) — stored + returned, never
    searched or faceted. Deliberately self-contained: the SPA reads the whole
    card off one hit, with no follow-up call to /api/cases/{slug}/.
    """
    return {
        "slug": slug,
        "title": title,
        "short_description": short.strip() if short and short.strip() else None,
        "key_allegations": _key_allegations(case),
        "tags": tags,
        "case_type": case_type,
        "status": case_status,
        "case_start_date": _iso(getattr(case, "case_start_date", None)),
        "case_end_date": _iso(getattr(case, "case_end_date", None)),
        "bigo": getattr(case, "bigo", None),
        "thumbnail_url": getattr(case, "thumbnail_url", None),
        "banner_url": getattr(case, "banner_url", None),
        "timeline": [
            entry
            for entry in (getattr(case, "timeline", None) or [])
            if isinstance(entry, dict)
        ],
        "entities": list(entities or []),
    }


def build_doc(
    case: Any,
    *,
    entities: list[dict[str, Any]] | None = None,
    tags: tuple[list[str], list[str]] | None = None,
) -> dict[str, Any]:
    """Map a published ``Case`` to the common index doc. Pure: no OpenSearch, no DB.

    The caller is responsible for the published gate; this shapes whatever it is
    given (used directly by tests for the doc shape). ``entities`` is the resolved
    entity-bind list for the ``card`` payload — ``index()`` resolves it via
    :func:`_safe_resolve_entities`; passing ``None`` (the default) omits names so
    the doc-shape tests stay pure/DB-free.

    ``tags`` is the same arrangement for the tag roll-up: ``(ids, labels)`` from
    :func:`_expand_tags`, which reads the vocabulary table. Injected rather than
    looked up here so this stays DB-free; ``None`` falls back to the case's own tags
    with no roll-up and the ids doubling as labels."""
    iri = _case_iri(case)
    title = getattr(case, "title", "") or ""
    title_ne, title_en = name_to_titles(title)

    short = getattr(case, "short_description", None)
    body = _build_body(case, short)

    case_tags_raw = [t for t in (getattr(case, "tags", None) or []) if isinstance(t, str)]
    # Two DIFFERENT jobs, so two different values.
    #
    # ``tags`` (top-level keyword, added below) carries canonical IDS plus every
    # broader tag, and is what ``?tags=`` filters and facets on. The roll-up happens
    # HERE rather than at query time so ``?tags=land`` matches a case tagged only
    # ``land-grab`` with a plain term query and no query rewriting.
    #
    # ``keywords`` (analyzed text) carries the LABELS. Putting the slug
    # `land-administration` into a text field helps nobody -- a reader types
    # "भूमि प्रशासन" or "land administration". Before the vocabulary, tags WERE their
    # own display text so the raw string served both jobs; canonical ids do not.
    expanded, labels = tags if tags is not None else (list(case_tags_raw), list(case_tags_raw))
    keywords = list(labels)
    case_type = getattr(case, "case_type", None)
    if case_type:
        keywords.append(case_type)

    slug = getattr(case, "slug", None)
    identifiers = _build_identifiers(case, iri, slug)

    doc: dict[str, Any] = {
        "iri": iri,
        "type": TYPE_TOKEN,
        "source_app": SOURCE_APP,
        "title_ne": title_ne,
        "title_en": title_en,
        "title_translit": title_translit(title_ne, title_en),
        "body": body,
        "keywords": keywords,
        "tags": expanded,
        "identifiers": identifiers,
        "raw": {
            "@id": iri,
            "slug": slug,
            "case_type": case_type,
            "title": title,
            "tags": case_tags_raw,
        },
    }
    # Promote case_type to a top-level keyword so the unified search can filter and
    # facet on it (it also stays in ``keywords`` and ``raw`` for text recall).
    # NORMALIZE to upper-case to share ONE facet vocabulary with the NGM courtcase
    # docs (courts/search_index.py also upper-cases): a Jawafdehi ``CORRUPTION``
    # case and a court case typed "Corruption" must land in the SAME facet bucket,
    # and the ``?case_type=`` filter (which upper-cases too) must match both. The
    # CaseType enum is already upper-case; this guards against any non-enum value.
    if case_type and isinstance(case_type, str):
        doc["case_type"] = case_type.upper()

    # ``getattr`` with a default because build_doc is pure — it shapes whatever it
    # is given, including objects (and fixtures) predating the field.
    doc["weight"] = int(getattr(case, "weight", 0) or 0)

    _apply_dates(doc, case)

    # Coarse lifecycle as a dedicated indexed keyword so the unified search can
    # facet/filter cases on it. Deliberately NOT the generic ``status`` field —
    # NGM courtcases write their scraper enrichment flag (pending/enriched/failed)
    # there, which must not blend into a case lifecycle facet.
    case_status = _derive_status(case)
    doc["case_status"] = case_status
    # Also in ``raw`` so ``_serialize_hit`` surfaces it as ``extra.case_status``
    # (the SPA's non-card fallback for a hit's lifecycle).
    doc["raw"]["case_status"] = case_status

    # बिगो promoted to a top-level ``long`` so the unified search can range-filter
    # on it (?bigo_min/?bigo_max). The card keeps its own copy for rendering; that
    # one lives under ``raw`` and is NOT indexed. Set only when recorded — an
    # absent field is excluded by a range clause, which is exactly right for a case
    # with no known amount, and avoids a null the ``long`` mapping would reject.
    bigo = _bigo(case)
    if bigo is not None:
        doc["bigo"] = bigo

    doc["raw"]["card"] = _build_card(
        case,
        slug=slug,
        title=title,
        short=short,
        tags=case_tags_raw,
        case_type=case_type,
        case_status=case_status,
        entities=entities,
    )
    return doc


def _safe_expand_tags(case: Any) -> tuple[list[str], list[str]]:
    """:func:`_expand_tags` for the real index paths, best-effort.

    Same contract as :func:`_safe_resolve_entities`: any failure — a bare test
    object with no vocabulary behind it, or the table unreachable — falls back to
    the case's own tags with no roll-up, so the case is still INDEXED and a later
    ``reindex_cases`` reconciles the expansion. Losing the roll-up costs one facet
    match; losing the document takes a published case out of search entirely.
    """
    raw = [t for t in (getattr(case, "tags", None) or []) if isinstance(t, str)]
    try:
        return _expand_tags(raw)
    except Exception:  # noqa: BLE001 — best-effort, same as the entity resolver.
        return list(raw), list(raw)


def build_indexed_doc(case: Any) -> dict[str, Any]:
    """``build_doc`` for the real index paths: resolves entity names first.

    ``build_doc`` stays pure (entities injected) so the shape tests need no DB;
    this resolving wrapper is what BOTH the live ``index()`` signal path AND the
    bulk ``reindex_cases`` driver call, so a rebuild REFRESHES the denormalized
    entity names rather than blanking them (the driver calls ``build_doc``
    positionally with no ``entities`` kwarg — see ``jawafdehi_shared/search/
    reindex.py``)."""
    return build_doc(
        case,
        entities=_safe_resolve_entities(case),
        tags=_safe_expand_tags(case),
    )


def index_now(case: Any, *, client=None) -> None:
    """Upsert a PUBLISHED case; otherwise remove it from the index. RAISES.

    The case-only-published rule: publishing indexes; any non-PUBLISHED state
    (draft/in-review/closed) deletes the doc so it never appears in search.

    The raising twin of :func:`index`, for the one caller that has somewhere to
    put a failure. Write-time signal paths want best-effort — an OpenSearch blip
    must not fail a case save — but a caller with a retry budget and a dead
    letter queue (``case_events.consumers.handlers.handle_derive``) needs the
    error, and swallowing it turns that budget into decoration."""
    cl = client or make_client()
    if should_index(case):
        upsert_doc(cl, CASE_INDEX, build_indexed_doc(case))
    else:
        delete_now(case, client=cl)


def evictable_iri(case: Any) -> str | None:
    """The IRI a case's doc is keyed by, even once it has LEFT published state.

    ``public_iri`` returns None for a non-published case, so eviction has to fall
    back to rebuilding the IRI from the slug — otherwise a case that has just been
    unpublished cannot be addressed to delete it. Used by ``delete_now`` and by
    the rebuild catch-up, which needs the same answer for a tombstone."""
    iri = _case_iri(case)
    if iri:
        return iri
    slug = getattr(case, "slug", None)
    if not slug:
        return None
    from jawafdehi_shared.entities.ids import build_case_iri

    try:
        return build_case_iri(slug)
    except ValueError:
        return None


def delete_now(case: Any, *, client=None) -> None:
    """Delete the case's doc from ``jawafdehi-cases``. RAISES."""
    iri = evictable_iri(case)
    if iri:
        delete_doc(client or make_client(), CASE_INDEX, iri)


#: The best-effort forms — what every write-time signal path wants, and the
#: names every existing caller already imports. Derived from the raising twins
#: rather than the other way round, so there is one implementation of the rule
#: and the swallow is visibly a wrapper over it.
#:
#: **Patching note for tests.** These are bound at import, so
#: ``mock.patch("cases.search_index.index_now")`` does NOT intercept a caller
#: that went through ``index`` — the wrapper closed over the original function
#: object. Patch the name the code under test actually calls: ``index`` for the
#: write-time signal paths, ``index_now`` for the bus consumer.
index = best_effort("index case")(index_now)
delete = best_effort("delete case")(delete_now)

# best_effort copies __name__/__doc__ off the wrapped function, so without this
# `index` introspects as "index_now" and carries a docstring beginning "RAISES."
# — the opposite of what it does. Anything reading help() or a traceback frame
# would be told the wrong contract about the more widely used of the two.
index.__name__ = "index"
index.__doc__ = "Upsert a PUBLISHED case, else evict it. Best-effort: logs and swallows. See index_now."
delete.__name__ = "delete"
delete.__doc__ = "Delete the case's doc. Best-effort: logs and swallows. See delete_now."
