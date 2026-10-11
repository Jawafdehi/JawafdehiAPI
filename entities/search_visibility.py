"""Public-search visibility gate for NES entities.

The entity store is the only record type that entered public unified search
ungated: 187,464 documents, of which 1,544 (0.8%) are referenced by a PUBLISHED
Jawafdehi case. The rest is bulk-imported registry (165k persons, 11k hospitals,
8.6k government bodies) that no case has ever cited, and it crowded out the
curated corpus on every browse and in every facet.

This module answers ONE question: how many PUBLISHED Jawafdehi cases cite this
entity? The entity indexer promotes that number onto the search document as
``case_count`` and the unified search filters on it, so an entity is publicly
searchable iff a published case names it.

DERIVED, NOT STORED. There is deliberately no ``is_archived`` column:

* nothing is deleted or mutated, so "recover an archived entity" is not a code
  path — the ``StoredEntity`` row was never touched;
* an entity becomes visible the moment a published case binds it, with no
  operator step, because the count is recomputed from the binds themselves
  (``cases.signals`` reindexes on every bind write);
* the stored value cannot disagree with reality for longer than one signal, and
  ``reconcile_entity_visibility`` closes even that window.

Every bind role counts, ``location`` included (decision 2026-09-09). A bind on a
DRAFT / IN_REVIEW / CLOSED case does not count, mirroring the case index's own
PUBLISHED-only rule.

NOT the reverse gate: NGM court cases do NOT contribute references. The
``courtcase -> entity`` edge is unpopulated (``nes_resolved: 0.0``), so counting
it today would add nothing; when the party resolver runs, add it here and
nowhere else.

FAILURE POSTURE, deliberately split by blast radius:

* :func:`case_counts` (the BULK path, feeding a whole reindex) RAISES on a
  cases-DB failure. It must never degrade to "nothing is referenced", because
  that answer archives the entire corpus in one pass. Note this is belt AND
  braces: ``jawafdehi_shared.search.reindex.RebuildAborted`` already refuses to
  swap a generation that came out empty, precisely to catch a broken visibility
  gate. Raising here fails earlier and names the real cause.
* :func:`entity_case_count` (the LIVE path, one document) logs and returns 0.
  A transient blip mis-indexes one entity, and the hourly reconcile repairs it.
  Raising instead would abort the whole entity save's best-effort indexing.

Contrast with ``courts.search_visibility``, which swallows to an empty set on
both paths: its gate has independent code/forum rules that still admit documents
when the publish-link lookup fails, so an empty set degrades gracefully there.
This gate has no such fallback rule, so an empty set here means an empty index.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from django.core.cache import cache
from django.db.models import Count

logger = logging.getLogger(__name__)

#: Cache key for the referenced-IRI set. Versioned so a shape change (e.g.
#: counting court-case parties) cannot read a stale payload written by the old
#: definition.
CACHE_KEY = "entities:referenced_iris:v1"

#: Cache TTL. Short, because the authoritative invalidation is
#: :func:`clear_cache` from ``cases.signals``; the TTL only bounds how long a
#: process can be wrong if a signal is lost or fires in another pod. Redis is
#: the configured backend in every deployed environment, so the invalidation is
#: cross-process; the LocMemCache fallback (tests, DB-less local) makes it
#: per-process, which the TTL then covers.
CACHE_TTL_SECONDS = 300


class ReferenceLookupError(RuntimeError):
    """The referenced-entity set could not be read from the cases database.

    Raised only by the bulk path (:func:`case_counts`), where answering "nothing
    is referenced" would archive every entity at once.
    """


def _published_bind_queryset():
    """Entity binds held by PUBLISHED cases. Imported lazily: ``entities`` must
    stay importable without pulling the cases app at module load."""
    from cases.models import CaseEntityRelationship, CaseState

    return CaseEntityRelationship.objects.filter(case__state=CaseState.PUBLISHED)


def referenced_iris(*, refresh: bool = False) -> frozenset[str]:
    """Entity ``@id`` IRIs cited by at least one PUBLISHED Jawafdehi case.

    Cached (see :data:`CACHE_TTL_SECONDS`). ~1,544 entries today. Used by the
    reconcile command and available to callers that need set membership rather
    than a count.

    Raises :class:`ReferenceLookupError` if the cases DB cannot be read — this is
    a bulk answer, so the empty-set degradation is not safe here.
    """
    if not refresh:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return frozenset(cached)
    try:
        iris = frozenset(
            _published_bind_queryset().values_list("nes_id", flat=True).distinct()
        )
    except Exception as exc:  # noqa: BLE001 — re-raised as a typed error below.
        raise ReferenceLookupError(
            "could not read the published entity-bind set from the cases DB"
        ) from exc
    # Store a list: the Redis backend pickles, but a frozenset round-trip is not
    # worth relying on across backends.
    cache.set(CACHE_KEY, sorted(iris), CACHE_TTL_SECONDS)
    return iris


def case_counts() -> dict[str, int]:
    """``{entity IRI: number of PUBLISHED cases citing it}`` for every cited IRI.

    The BULK path. One grouped query (~1,544 rows), loaded once by
    ``reindex_entities`` so streaming 187k documents costs no per-document query.
    An IRI absent from the mapping has a count of 0.

    Counts DISTINCT cases, not binds: a case that names the same person as both
    ``accused`` and ``related`` cites them once, and "in 2 cases" must mean two
    cases.

    Raises :class:`ReferenceLookupError` if the cases DB cannot be read.
    """
    try:
        rows = (
            _published_bind_queryset()
            .values("nes_id")
            .annotate(case_count=Count("case", distinct=True))
        )
        return {row["nes_id"]: row["case_count"] for row in rows}
    except Exception as exc:  # noqa: BLE001 — re-raised as a typed error below.
        raise ReferenceLookupError(
            "could not read published entity-bind counts from the cases DB"
        ) from exc


def entity_case_count(iri: str | None) -> int:
    """PUBLISHED cases citing ``iri``. The LIVE (single-document) path.

    Logs and returns 0 on a DB failure rather than raising: the caller is
    best-effort indexing from a ``post_save``, and one mis-indexed entity that
    the reconcile repairs beats aborting the whole save's indexing. See the
    module docstring's failure posture.
    """
    if not iri:
        return 0
    try:
        return (
            _published_bind_queryset()
            .filter(nes_id=iri)
            .values("case")
            .distinct()
            .count()
        )
    except Exception:  # noqa: BLE001 — best-effort; reconcile repairs the drift.
        logger.warning("entity_case_count failed for %s; indexing as 0", iri, exc_info=True)
        return 0


def entity_public_visible(entity: Any, *, case_count: int | None = None) -> bool:
    """Whether ``entity`` belongs in PUBLIC unified-search results.

    Both conditions must hold: the entity is live on the read plane
    (``is_deleted`` false — the existing eviction rule in ``entities.signals``),
    and at least one PUBLISHED case cites it.

    The document is still INDEXED when this is false; the gate is applied at
    query time by ``search.service`` so authorized callers (the caseworker entity
    picker) can still find an unreferenced entity in order to bind it. Removing
    the document instead would deadlock: no picker hit means no bind, and no bind
    means the entity can never become visible.
    """
    if getattr(entity, "is_deleted", False):
        return False
    if case_count is None:
        case_count = entity_case_count(
            getattr(entity, "iri", None) or (getattr(entity, "data", None) or {}).get("@id")
        )
    return case_count > 0


def clear_cache() -> None:
    """Invalidate the referenced-IRI set. Called from ``cases.signals`` on any
    bind write or case state change."""
    cache.delete(CACHE_KEY)


def iter_missing_iris(counts: dict[str, int], known: Iterable[str]) -> list[str]:
    """IRIs cited by a published case but absent from ``known``.

    Used by ``reconcile_entity_visibility`` to report binds pointing at an entity
    the store does not hold (a dangling ``nes_id`` — possible for a bind written
    before the entity, or after a hard delete outside the merge path).
    """
    return sorted(set(counts) - set(known))
