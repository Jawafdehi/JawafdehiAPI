"""Django-ORM persistence for NES schema.org JSON-LD entity documents.

CLEAN-SLATE remodel (2026-06-28): the stored form is a raw schema.org JSON-LD
document in ``data`` (JSONB), keyed by the canonical ``@id`` IRI. The promoted
columns (``entity_type``/``prefix``/``slug``/``version``/timestamps) are derived
from the document on write. There is no per-type Pydantic reconstruction —
``get_entity`` returns the stored JSON-LD dict verbatim.

This is the Django-native repository the DRF views + bulk-ingest + publication
service call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from django.db import connection
from django.db.models import Q, QuerySet
from jawafdehi_shared.entities.ids import canonicalize_entity_iri, parse_entity_iri

from .models import HeldEntity, StoredAuthor, StoredEntity, StoredVersion
from .validation import primary_type

# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

MAX_LIMIT = 1000
# Textual query has to be scored in Python (no search backend yet); cap the
# candidate window so the public search endpoint can't drive an unbounded scan.
MAX_SEARCH_CANDIDATES = 5000


def _clamp_limit(limit: int) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        return 100
    return max(1, min(limit, MAX_LIMIT))


def _clamp_offset(offset: int) -> int:
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        return 0
    return max(0, offset)


def _supports_jsonb_containment() -> bool:
    """Postgres supports ``@>`` containment lookups; sqlite (tests) does not."""
    return connection.vendor == "postgresql"


def _canonicalize_doc_id(doc: Dict[str, Any]) -> str:
    """Rewrite ``doc['@id']`` to the canonical authority+scheme, in place.

    The scheme+host is part of the join key, so any valid-shaped @id is re-keyed
    onto :func:`iri_base` before it is stored — two services persisting
    ``http://evil.com/...`` and ``https://jawafdehi.org/...`` must collide on one
    canonical PK. Returns the canonical IRI. Raises ValueError on a malformed @id.
    """
    canonical = canonicalize_entity_iri(doc["@id"])
    doc["@id"] = canonical
    return canonical


def _entity_row_fields(doc: Dict[str, Any], *, version: int, created_at: datetime,
                       updated_at: datetime) -> Dict[str, Any]:
    """Promoted-column values + ``data`` for an entity row, derived from the
    JSON-LD document. The @id is canonicalized to :func:`iri_base` (in place on
    ``doc``) so the stored PK and ``data['@id']`` share one authority."""
    iri = _canonicalize_doc_id(doc)
    parsed = parse_entity_iri(iri)
    return {
        "entity_type": primary_type(doc),
        "prefix": parsed.prefix,
        "slug": parsed.slug,
        "data": doc,
        "version": version,
        "created_at": created_at,
        "updated_at": updated_at,
        # A (re-)publish revives a soft-deleted row: the write is the source of
        # truth, so clear the soft-delete flag on every upsert.
        "is_deleted": False,
    }


def _apply_entity_filters(
    qs: "QuerySet",
    *,
    entity_type: Optional[str],
    prefix: Optional[str],
) -> "QuerySet":
    """Push the promoted-column filters (type/prefix) into SQL."""
    if entity_type is not None:
        # @type may be stored as a comma-joined list; match membership.
        qs = qs.filter(
            Q(entity_type=entity_type)
            | Q(entity_type__startswith=entity_type + ",")
            | Q(entity_type__endswith="," + entity_type)
            | Q(entity_type__contains="," + entity_type + ",")
        )
    if prefix is not None:
        # startswith logic: exact prefix or prefix + '/'.
        qs = qs.filter(Q(prefix=prefix) | Q(prefix__startswith=prefix + "/"))
    return qs


class EntityRepository:
    """Synchronous Django-ORM repository over the JSON-LD entity store.

    All read methods return the stored JSON-LD dict (the ``data`` column);
    writes take a JSON-LD document keyed by ``@id``.
    """

    # --- entities ---------------------------------------------------------

    @staticmethod
    def _live() -> "QuerySet":
        """Base queryset excluding soft-deleted rows (the read contract)."""
        return StoredEntity.objects.filter(is_deleted=False)

    def get_entity(self, iri: str) -> Optional[Dict[str, Any]]:
        return (
            self._live()
            .filter(pk=iri)
            .values_list("data", flat=True)
            .first()
        )

    def put_entity(
        self, doc: Dict[str, Any], *, version: int, created_at: datetime
    ) -> Dict[str, Any]:
        # _entity_row_fields canonicalizes doc['@id'] in place; build the row
        # first so the PK (iri=) is the canonical form, not the raw input host.
        fields = _entity_row_fields(
            doc,
            version=version,
            created_at=created_at,
            updated_at=datetime.now(timezone.utc),
        )
        StoredEntity.objects.update_or_create(iri=doc["@id"], defaults=fields)
        return doc

    def bulk_put_entities(self, docs: List[Dict[str, Any]], *, versions: Dict[str, int],
                          created_ats: Dict[str, datetime]) -> int:
        """Upsert many JSON-LD docs. Idempotent by @id; the caller wraps this in
        a transaction. ``versions``/``created_ats`` are keyed by @id IRI."""
        now = datetime.now(timezone.utc)
        for doc in docs:
            # versions/created_ats are keyed by the caller's @id; capture it
            # before _entity_row_fields canonicalizes doc['@id'] in place.
            raw_iri = doc["@id"]
            fields = _entity_row_fields(
                doc,
                version=versions[raw_iri],
                created_at=created_ats[raw_iri],
                updated_at=now,
            )
            StoredEntity.objects.update_or_create(iri=doc["@id"], defaults=fields)
        return len(docs)

    def delete_entity(self, iri: str) -> bool:
        """Soft-delete: flip ``is_deleted=True`` (never hard-delete — this is an
        accountability/audit platform). Returns True iff a live row was flipped.
        The ORM ``save()`` fires the search-index signal, which evicts the row."""
        entity = self._live().filter(pk=iri).first()
        if entity is None:
            return False
        entity.is_deleted = True
        entity.save(update_fields=["is_deleted", "updated_at"])
        return True

    def entity_version(self, iri: str) -> Optional[int]:
        """The current promoted version number for a live entity (or None)."""
        return (
            self._live()
            .filter(pk=iri)
            .values_list("version", flat=True)
            .first()
        )

    def entity_created_at(self, iri: str) -> Optional[datetime]:
        return (
            self._live()
            .filter(pk=iri)
            .values_list("created_at", flat=True)
            .first()
        )

    def count_entities(
        self,
        *,
        entity_type: Optional[str] = None,
        prefix: Optional[str] = None,
    ) -> int:
        qs = _apply_entity_filters(
            self._live(), entity_type=entity_type, prefix=prefix
        )
        return qs.count()

    def _search_queryset(
        self,
        *,
        entity_type: Optional[str],
        prefix: Optional[str],
        keywords: Optional[List[str]],
    ) -> tuple["QuerySet", bool]:
        """Build the filtered queryset, pushing as much into SQL as supported.

        Returns ``(queryset, keywords_pushed_down)``. Promoted-column filters go
        to SQL always; ``keywords`` (schema.org ``keywords`` array) containment is
        pushed via JSONField lookup on Postgres, filtered in Python on sqlite.
        """
        qs = _apply_entity_filters(
            self._live(), entity_type=entity_type, prefix=prefix
        )
        keywords_pushed_down = False
        if keywords and _supports_jsonb_containment():
            qs = qs.filter(data__keywords__contains=list(keywords))
            keywords_pushed_down = True
        return qs, keywords_pushed_down

    def search_entities(
        self,
        *,
        query: Optional[str] = None,
        entity_type: Optional[str] = None,
        prefix: Optional[str] = None,
        keywords: Optional[List[str]] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        """SQL/JSONB search returning stored JSON-LD docs.

        Promoted filters + keyword containment are pushed into SQL (GIN on
        Postgres). A textual ``query`` is scored in Python over a bounded
        candidate window.
        """
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)

        qs, keywords_pushed_down = self._search_queryset(
            entity_type=entity_type, prefix=prefix, keywords=keywords
        )
        qs = qs.order_by("iri")

        if not query:
            if keywords and not keywords_pushed_down:
                wanted = set(keywords)
                matched: List[Dict[str, Any]] = []
                for data in qs.values_list("data", flat=True).iterator():
                    if wanted.issubset(set(data.get("keywords") or [])):
                        matched.append(data)
                    if len(matched) >= offset + limit:
                        break
                return matched[offset : offset + limit]
            return list(qs.values_list("data", flat=True)[offset : offset + limit])

        # Textual query: score in Python over a CAPPED candidate set.
        needle = query.lower()
        wanted = set(keywords) if (keywords and not keywords_pushed_down) else None
        candidates: List[Dict[str, Any]] = []
        for data in qs.values_list("data", flat=True)[:MAX_SEARCH_CANDIDATES].iterator():
            if wanted is not None and not wanted.issubset(set(data.get("keywords") or [])):
                continue
            candidates.append(data)

        scored = [(d, _relevance_score(d, needle)) for d in candidates]
        scored = [(d, s) for d, s in scored if s > 0]
        scored.sort(key=lambda x: (-x[1], x[0].get("@id", "")))
        return [d for d, _ in scored][offset : offset + limit]

    def count_search(
        self,
        *,
        entity_type: Optional[str] = None,
        prefix: Optional[str] = None,
        keywords: Optional[List[str]] = None,
    ) -> int:
        qs, keywords_pushed_down = self._search_queryset(
            entity_type=entity_type, prefix=prefix, keywords=keywords
        )
        if keywords and not keywords_pushed_down:
            wanted = set(keywords)
            count = 0
            for data in qs.values_list("data", flat=True).iterator():
                if wanted.issubset(set(data.get("keywords") or [])):
                    count += 1
            return count
        return qs.count()

    def all_prefixes(self) -> List[str]:
        """Distinct entity prefixes across all entities (for /api/entity_prefixes)."""
        return sorted(
            self._live().values_list("prefix", flat=True).distinct()
        )

    def all_keywords(self) -> List[str]:
        """Distinct schema.org ``keywords`` across all entities (for /api/entities/tags)."""
        if connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT kw "
                    "FROM entities, "
                    "LATERAL jsonb_array_elements_text(data->'keywords') AS kw "
                    "WHERE jsonb_typeof(data->'keywords') = 'array' "
                    "AND NOT is_deleted "
                    "ORDER BY kw"
                )
                return [row[0] for row in cursor.fetchall()]

        kws: set = set()
        for data in self._live().values_list("data", flat=True).iterator():
            for kw in (data.get("keywords") or []):
                if isinstance(kw, str):
                    kws.add(kw)
        return sorted(kws)

    # --- versions ---------------------------------------------------------

    def put_version(
        self, *, iri: str, version_number: int, author_id: str,
        snapshot: Dict[str, Any], created_at: datetime
    ) -> None:
        StoredVersion.objects.update_or_create(
            id=f"version:{iri}:{version_number}",
            defaults={
                "subject_iri": iri,
                "version_number": version_number,
                "author_id": author_id,
                "data": snapshot,
                "created_at": created_at,
            },
        )

    def list_versions_by_entity(
        self, iri: str, *, limit: int = 1000, offset: int = 0
    ) -> List[Dict[str, Any]]:
        limit = _clamp_limit(limit)
        offset = _clamp_offset(offset)
        rows = (
            StoredVersion.objects.filter(subject_iri=iri)
            .order_by("version_number")
            .values("version_number", "author_id", "data", "created_at")[
                offset : offset + limit
            ]
        )
        return [
            {
                "entity_iri": iri,
                "version_number": r["version_number"],
                "author_id": r["author_id"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "snapshot": r["data"],
            }
            for r in rows
        ]

    def count_versions_by_entity(self, iri: str) -> int:
        return StoredVersion.objects.filter(subject_iri=iri).count()

    # --- authors ----------------------------------------------------------

    def get_author(self, author_id: str) -> Optional[Dict[str, Any]]:
        return (
            StoredAuthor.objects.filter(pk=author_id)
            .values_list("data", flat=True)
            .first()
        )

    def put_author(self, author_id: str, data: Dict[str, Any]) -> None:
        StoredAuthor.objects.update_or_create(id=author_id, defaults={"data": data})

    # --- held entities (bulk-ingest HOLD staging) -------------------------

    def stage_held_entities(self, held: List[Dict[str, Any]]) -> int:
        now = datetime.now(timezone.utc)
        for h in held:
            HeldEntity.objects.update_or_create(
                iri=h["iri"],
                defaults={
                    "entity_data": h.get("entity_data") or {},
                    "sources": h.get("sources") or [],
                    "reason": h.get("reason"),
                    "created_at": now,
                },
            )
        return len(held)

    def get_held_entity(self, iri: str) -> Optional[Dict[str, Any]]:
        row = HeldEntity.objects.filter(pk=iri).first()
        if row is None:
            return None
        return {
            "iri": row.iri,
            "entity_data": row.entity_data,
            "sources": row.sources,
            "reason": row.reason,
            "created_at": row.created_at,
        }


def _relevance_score(doc: Dict[str, Any], needle: str) -> float:
    """Lightweight name/keyword relevance for the no-backend fallback search.

    Scores over the JSON-LD ``name``/``alternateName`` (string or language map)
    and ``keywords``.
    """
    score = 0.0

    def _score_name(value: Any, weight_exact: float, weight_sub: float) -> float:
        s = 0.0
        texts: List[str] = []
        if isinstance(value, str):
            texts = [value]
        elif isinstance(value, dict):
            texts = [v for v in value.values() if isinstance(v, str)]
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, dict):
                    texts.extend(v for v in item.values() if isinstance(v, str))
        for t in texts:
            low = t.lower()
            if low == needle:
                s += weight_exact
            elif needle in low:
                s += weight_sub
        return s

    score += _score_name(doc.get("name"), 10.0, 3.0)
    score += _score_name(doc.get("alternateName"), 5.0, 2.0)
    for kw in (doc.get("keywords") or []):
        if isinstance(kw, str) and needle in kw.lower():
            score += 1.0
    return score
