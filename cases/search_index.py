"""Unified-search indexer for Jawafdehi cases (``jawafdehi-cases`` index).

Projects a ``Case`` into the common index doc. CASE-ONLY-PUBLISHED RULE (plan
§5, decision #6): only ``state == PUBLISHED`` cases enter the index — the search
index is all-public, so there is NO visibility/ACL field. ``index(case)`` upserts
when published and DELETES from the index when the case is not published (so a
case that leaves PUBLISHED is removed). ``should_index(case)`` exposes the rule.

Field mapping:
* ``iri``            ← ``Case.public_iri`` (``https://jawafdehi.org/case/<slug>``,
  minted at publish — the document ``_id``),
* ``type``           ← ``"Case"`` (+ the platform ``offence_type`` in keywords),
* ``title_ne/en``    ← ``Case.title`` (script-bucketed; cases store one title),
* ``title_translit`` ← shared transliteration of the title,
* ``body``           ← ``description`` + ``key_allegations`` (joined),
* ``keywords``       ← ``tags`` (+ ``offence_type``),
* ``identifiers``    ← the IRI, the slug, and the ``court_cases`` references,
* ``date``           ← ``proceedings_started_on`` (else created date),
* ``status``         ← ``Case.status``, the full derived lifecycle,
* ``case_status``    ← the SAME status mapped down to the legacy three values
  (ongoing/closed/others) the deployed SPA facet knows — see
  :data:`LEGACY_CASE_STATUS`,
* ``case_track``     ← ``Case.case_track`` (omitted while unclassified),
* ``proceedings_started_on`` / ``proceedings_decided_on`` ← the derived stage
  dates, each omitted when null,
* ``bigo``           ← ``Case.bigo`` (बिगो, whole NPR) promoted to a top-level
  ``long`` so the unified search can RANGE-filter on it; the card copy under
  ``raw`` is return-only (``raw`` is ``enabled: false``, hence unqueryable),
* ``raw``            ← a light record PLUS a ``card`` payload (return-only): every
  field the SPA case list/card renders — ``short_description``, ``key_allegations``,
  ``tags``, dates, the ``stages`` list, ``bigo``, thumbnail/banner, the ``timeline``
  (major events), and the resolved entity binds — denormalized so a search hit
  renders WITHOUT a second fetch to ``/api/cases/{slug}/``.

The stage document lives ONLY under ``raw`` (mapped ``enabled: false``). It is
display data: promoting it would make the shape of a JSON blob part of the query
contract and have OpenSearch dynamically map every key inside it. The two
derived DATES are the queryable surface, which is exactly why they are columns.

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

import logging
from typing import Any

from jawafdehi_shared.search.indexing import (
    best_effort,
    delete_doc,
    name_to_titles,
    title_translit,
    upsert_doc,
)
from jawafdehi_shared.search.opensearch import CASE_INDEX, make_client

from .image_serializers import CARD_SPECS, SrcsetRenditionField
from .validators import parse_courtcase_ref

logger = logging.getLogger(__name__)

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


#: ``Case.status`` (six values, derived from the stage list plus the accused
#: outcomes) mapped down to the THREE the deployed SPA's ``case_status`` facet
#: knows. The SPA sends ``?status=ongoing|closed|others``; a fourth value in
#: that field would be a bucket no deployed client can select and a filter that
#: silently matches nothing, so the old vocabulary is held fixed here and the
#: full one goes to the new ``status`` field alongside.
#:
#: The two non-obvious arms:
#:
#: * ``withdrawn`` → ``closed``. The prosecution is over. Not ``others``: the
#:   case is as finished as a convicted one, and burying it in the catch-all
#:   would drop it out of the "closed" list a reader browses.
#: * ``dormant`` → ``others``, NOT ``ongoing``. A dormant case has an open stage
#:   and no decision coming (मिसिल जलेको, a bench never constituted); the
#:   override exists precisely to stop the open stage reading as live, so
#:   mapping it back to ``ongoing`` would reinstate the lie. ``closed`` is
#:   equally wrong — nothing was decided — which leaves the honest catch-all.
#:
#: ``under_investigation`` → ``others`` needs no argument: it is what the old
#: two-date rule already produced for a case with no court dates at all.
#:
#: Pinned arm-by-arm in ``search/tests/test_indexers.py`` and guarded against
#: model drift by ``tests/cases/test_case_status_derivation.py``.
LEGACY_CASE_STATUS: dict[str, str] = {
    "ongoing": "ongoing",
    "under_investigation": "others",
    "concluded": "closed",
    "others": "others",
    "withdrawn": "closed",
    "dormant": "others",
}

#: Where an unknown or unreadable status lands, in BOTH fields' terms.
_STATUS_FALLBACK = "others"


def _status(case: Any) -> str:
    """``Case.status`` as a plain string, or ``others``.

    ``status`` is a PROPERTY that reads the case's accused binds, so unlike
    every other field here it can touch the database and it can raise (an
    unsaved instance has no primary key to follow the relation from). Guarded
    for the same reason ``_bigo`` and ``_card_srcset`` are: losing the lifecycle
    is recoverable, losing a published case from search is not.
    """
    try:
        value = getattr(case, "status", None)
    except Exception:  # noqa: BLE001 - a broken property must not fail a reindex
        logger.warning(
            "case_search_index.status_unreadable", extra={"case": _case_iri(case)}
        )
        return _STATUS_FALLBACK
    if not value or not isinstance(value, str):
        return _STATUS_FALLBACK
    return str(value)


def _legacy_case_status(status: str) -> str:
    """The three-value ``case_status`` for a full-vocabulary status."""
    legacy = LEGACY_CASE_STATUS.get(status)
    if legacy is None:
        # A lifecycle the model gained without anyone deciding its legacy
        # bucket. The drift test should have caught it; say so loudly rather
        # than teaching the deployed facet a fourth word.
        logger.warning(
            "case_search_index.unmapped_status", extra={"status": status}
        )
        return _STATUS_FALLBACK
    return legacy


def _stages(case: Any) -> list[dict[str, Any]]:
    """The stage records for display, or ``[]``.

    Total by design: a legacy row migrated unchanged must still render, exactly
    as ``derived_proceeding_dates`` tolerates one. Non-dict entries are dropped
    the way ``timeline`` drops them.
    """
    dates = getattr(case, "dates", None)
    if not isinstance(dates, dict):
        return []
    stages = dates.get("stages")
    if not isinstance(stages, list):
        return []
    return [record for record in stages if isinstance(record, dict)]


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

    ``date`` is the archive sort key, and it now follows
    ``proceedings_started_on`` -- the earliest start among COURT stages.

    This is a rename, not a resort. Measured against NGM on ten published CIAA
    cases, ``case_start_date`` has always HELD the court registration date
    (seven exact, three off by one day) whatever its help_text says, and the
    stage backfill seeds each ``initial`` stage's ``start`` from it. So
    ``proceedings_started_on`` equals the old sort key on every migrated row:
    the archive keeps the order it has today, under a name that says what the
    value is.

    The fallback to the creation date is deliberately kept: a case whose every
    court stage lacks a start legitimately has no proceeding start (a verdict
    date is often known from a court order when the registration date is not),
    and it must still land somewhere in a sorted list rather than dropping to
    ``missing: _last`` for good.
    """
    start = getattr(case, "proceedings_started_on", None)
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


def _apply_proceedings(doc: dict[str, Any], case: Any) -> None:
    """Set ``case_track`` and the two derived proceeding dates, when present.

    Each is OMITTED rather than emitted null, for two different reasons that
    happen to agree. A null against the ``date`` mapping is rejected by
    OpenSearch, which rejects the whole document with it -- the ``bigo`` lesson.
    And an absent field is what a ``range`` or ``terms`` clause should skip: a
    case with no decision yet genuinely does not belong in any decided-between
    bound, and an unclassified case belongs in no track.

    A blank ``case_track`` is treated as unset for the same reason the stub
    ``court_type`` is dropped in the courtcase indexer: an empty keyword is a
    real facet bucket with no name.
    """
    track = getattr(case, "case_track", None)
    if isinstance(track, str) and track.strip():
        doc["case_track"] = track
    for field in ("proceedings_started_on", "proceedings_decided_on"):
        value = getattr(case, field, None)
        if value is not None:
            doc[field] = _iso(value)


def _card_srcset(case: Any) -> dict[str, Any] | None:
    """The card image's responsive payload, or ``None`` if there isn't one.

    Indexing is where the card renditions get created, which is deliberate: it
    is a batch job, so the cost lands on a reindex rather than on the first
    visitor to a search results page.

    The URLs are ABSOLUTE and frozen at index time (``absolute_media_url``
    resolves them against ``MEDIA_PUBLIC_BASE`` / ``MEDIA_URL``). So a change of
    media host does not reach the search results until the next reindex — every
    indexed card keeps serving the old host, and the cards silently fall through
    to their placeholder because the old URLs no longer resolve. ``thumbnail_url``
    below has always had this property; the ladder just multiplies it by four.
    Treat a media-domain change as requiring ``reindex_cases --rebuild``, and
    note that the reindex is a separate process — it needs the media settings in
    its own environment, not just the web server's.

    Everything here is defensive because this module is written to shape
    whatever it is handed — the doc-shape tests pass plain stand-ins with no
    ``card_image``, and a reindex must not abort because object storage
    hiccuped on one case. Failure degrades to no image, never to an exception.
    """
    image = getattr(case, "card_image", None)
    if image is None:
        return None
    try:
        return SrcsetRenditionField(specs=CARD_SPECS).to_representation(image)
    except Exception:  # noqa: BLE001 - a broken image must not fail a reindex
        logger.warning(
            "case_search_index.rendition_failed", extra={"case": _case_iri(case)}
        )
        return None


def _build_card(
    case: Any,
    *,
    slug: str | None,
    title: str,
    short: str | None,
    tags: list[str],
    offence_type: Any,
    case_status: str,
    stages: list[dict[str, Any]],
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
        "offence_type": offence_type,
        # The deployed SPA card reads ``case_type``; emitted alongside the new
        # name until the frontend has moved. Drop with the other aliases.
        "case_type": offence_type,
        # The LEGACY three-value lifecycle: this is the deployed card badge.
        # The full vocabulary is the top-level ``status`` field.
        "status": case_status,
        "case_start_date": _iso(getattr(case, "case_start_date", None)),
        "case_end_date": _iso(getattr(case, "case_end_date", None)),
        # The stage list, for the labelled per-stage rows that replace the
        # single date range. Display only -- ``raw`` is ``enabled: false``, so
        # nothing in here is queryable; the derived dates on the doc are.
        "stages": stages,
        "proceedings_started_on": _iso(
            getattr(case, "proceedings_started_on", None)
        ),
        "proceedings_decided_on": _iso(
            getattr(case, "proceedings_decided_on", None)
        ),
        "bigo": getattr(case, "bigo", None),
        # The responsive card image. Denormalized like everything else here: a
        # search hit renders straight off the doc, so without this the results
        # page would fetch the full-size original for a 400px card.
        "thumbnail": _card_srcset(case),
        "thumbnail_url": getattr(case, "thumbnail_url", None),
        "banner_url": getattr(case, "banner_url", None),
        "timeline": [
            entry
            for entry in (getattr(case, "timeline", None) or [])
            if isinstance(entry, dict)
        ],
        "entities": list(entities or []),
    }


def build_doc(case: Any, *, entities: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Map a published ``Case`` to the common index doc. Pure: no OpenSearch.

    The caller is responsible for the published gate; this shapes whatever it is
    given (used directly by tests for the doc shape). ``entities`` is the resolved
    entity-bind list for the ``card`` payload — ``index()`` resolves it via
    :func:`_safe_resolve_entities`; passing ``None`` (the default) omits names so
    the doc-shape tests stay pure/DB-free."""
    iri = _case_iri(case)
    title = getattr(case, "title", "") or ""
    title_ne, title_en = name_to_titles(title)

    short = getattr(case, "short_description", None)
    body = _build_body(case, short)

    tags = [t for t in (getattr(case, "tags", None) or []) if isinstance(t, str)]
    keywords = list(tags)
    offence_type = getattr(case, "offence_type", None)
    if offence_type:
        keywords.append(offence_type)

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
        "identifiers": identifiers,
        "raw": {
            "@id": iri,
            "slug": slug,
            "offence_type": offence_type,
            "case_type": offence_type,  # DEPRECATED alias, see above
            "title": title,
            "tags": tags,
        },
    }
    # Promote offence_type to a top-level keyword so the unified search can filter and
    # facet on it (it also stays in ``keywords`` and ``raw`` for text recall).
    # NORMALIZE to upper-case to share ONE facet vocabulary with the NGM courtcase
    # docs (courts/search_index.py also upper-cases): a Jawafdehi ``CORRUPTION``
    # case and a court case typed "Corruption" must land in the SAME facet bucket,
    # and the ``?offence_type=`` filter (which upper-cases too) must match both. The
    # CaseType enum is already upper-case; this guards against any non-enum value.
    if offence_type and isinstance(offence_type, str):
        # ``case_type`` stays the facet field: the shared bucket with the NGM
        # courtcase docs is deliberate, and ``?case_type=`` filters both doc
        # types from one parameter. ``offence_type`` is additive so the frontend
        # can migrate before the facet is renamed in a later release.
        doc["case_type"] = offence_type.upper()
        doc["offence_type"] = offence_type.upper()

    # ``getattr`` with a default because build_doc is pure — it shapes whatever it
    # is given, including objects (and fixtures) predating the field.
    doc["weight"] = int(getattr(case, "weight", 0) or 0)

    _apply_dates(doc, case)

    # ONE source of truth for the lifecycle: the ``Case.status`` property. Two
    # fields carry it, because the deployed SPA cannot be redeployed in step
    # with the index:
    #
    # * ``case_status`` keeps the OLD three-value vocabulary the SPA filters and
    #   badges on (``?status=`` is wired to this field in
    #   ``search.service.FACET_FIELDS``). Teaching it a fourth value would give
    #   the facet a bucket no deployed client can select.
    # * ``status`` carries the full new vocabulary so the frontend can migrate
    #   on its own schedule.
    #
    # NB ``status`` is shared with the NGM courtcase docs, which write a scraper
    # enrichment flag (pending/enriched/failed) into it. Different indices, and
    # nothing aggregates the field today — but that is why the FACET is still
    # ``case_status`` and why faceting on ``status`` needs the two vocabularies
    # separated first.
    status = _status(case)
    case_status = _legacy_case_status(status)
    doc["status"] = status
    doc["case_status"] = case_status
    # Also in ``raw`` so ``_serialize_hit`` surfaces it as ``extra.case_status``
    # (the SPA's non-card fallback for a hit's lifecycle).
    doc["raw"]["case_status"] = case_status

    # The route into court and the two derived stage dates, so both are
    # filterable. The stage document they come from stays under ``raw``.
    _apply_proceedings(doc, case)

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
        tags=tags,
        offence_type=offence_type,
        case_status=case_status,
        stages=_stages(case),
        entities=entities,
    )
    return doc


def build_indexed_doc(case: Any) -> dict[str, Any]:
    """``build_doc`` for the real index paths: resolves entity names first.

    ``build_doc`` stays pure (entities injected) so the shape tests need no DB;
    this resolving wrapper is what BOTH the live ``index()`` signal path AND the
    bulk ``reindex_cases`` driver call, so a rebuild REFRESHES the denormalized
    entity names rather than blanking them (the driver calls ``build_doc``
    positionally with no ``entities`` kwarg — see ``jawafdehi_shared/search/
    reindex.py``)."""
    return build_doc(case, entities=_safe_resolve_entities(case))


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
