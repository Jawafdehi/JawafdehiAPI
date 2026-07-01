"""Publication Service — JSON-LD entities with automatic versioning.

CLEAN-SLATE remodel (2026-06-28): operates on schema.org JSON-LD documents keyed
by ``@id`` IRI, not Pydantic entity models. Orchestration is unchanged in intent:

- entity create/update with an automatic version-number bump + snapshot row,
- author get-or-create + attribution,
- minimal JSON-LD validation (@type known, @id valid IRI, name present).

The per-entity version metadata is recorded in the ``versions`` table and on the
document as ``jawafdehi:version`` (so the served JSON-LD carries its version).
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from jawafdehi_shared.entities.ids import canonicalize_entity_iri

from entities.persistence import EntityRepository
from entities.validation import validate_jsonld_entity


def _canonicalize_id(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of ``doc`` with ``@id`` re-keyed onto the canonical authority.

    The scheme+host is part of the join key, so any valid-shaped @id is normalized
    to ``iri_base()`` before validation/store — two services submitting the same
    path on different hosts converge on one canonical PK. A malformed @id is left
    untouched so ``validate_jsonld_entity`` raises the usual error.
    """
    iri = doc.get("@id")
    if isinstance(iri, str):
        try:
            canonical = canonicalize_entity_iri(iri)
        except ValueError:
            return doc
        if canonical != iri:
            doc = dict(doc)
            doc["@id"] = canonical
    return doc

UTC = timezone.utc
logger = logging.getLogger(__name__)


class PublicationService:
    """Publish and manage JSON-LD entities with automatic versioning."""

    def __init__(self, repo: Optional[EntityRepository] = None):
        self.repo = repo or EntityRepository()

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    def create_entity(
        self,
        doc: Dict[str, Any],
        author_id: str,
        change_description: str = "Initial entity creation",
    ) -> Dict[str, Any]:
        """Create a new entity (version 1) from a JSON-LD document."""
        doc = _canonicalize_id(doc)
        validate_jsonld_entity(doc)
        iri = doc["@id"]

        if self.repo.get_entity(iri) is not None:
            raise ValueError(f"Entity {iri} already exists")

        author = self._get_or_create_author(author_id)
        now = datetime.now(UTC)

        doc = dict(doc)
        doc["dateCreated"] = doc.get("dateCreated") or now.isoformat()
        doc["jawafdehi:version"] = _version_meta(
            iri, 1, author, change_description, now
        )

        self.repo.put_entity(doc, version=1, created_at=now)
        self.repo.put_version(
            iri=iri,
            version_number=1,
            author_id=author_id,
            snapshot=doc,
            created_at=now,
        )
        logger.info("Created entity %s version 1", iri)
        return doc

    def update_entity(
        self, doc: Dict[str, Any], author_id: str, change_description: str
    ) -> Dict[str, Any]:
        """Replace an existing entity's document, bumping its version."""
        doc = _canonicalize_id(doc)
        validate_jsonld_entity(doc)
        iri = doc["@id"]

        current_version = self.repo.entity_version(iri)
        if current_version is None:
            raise ValueError(f"Entity {iri} does not exist")
        created_at = self.repo.entity_created_at(iri) or datetime.now(UTC)

        author = self._get_or_create_author(author_id)
        new_version_number = current_version + 1
        now = datetime.now(UTC)

        doc = dict(doc)
        doc["jawafdehi:version"] = _version_meta(
            iri, new_version_number, author, change_description, now
        )

        self.repo.put_entity(doc, version=new_version_number, created_at=created_at)
        self.repo.put_version(
            iri=iri,
            version_number=new_version_number,
            author_id=author_id,
            snapshot=doc,
            created_at=now,
        )
        logger.info("Updated entity %s to version %d", iri, new_version_number)
        return doc

    def get_entity(self, iri: str) -> Optional[Dict[str, Any]]:
        return self.repo.get_entity(iri)

    def delete_entity(
        self, iri: str, author_id: str, change_description: str
    ) -> bool:
        result = self.repo.delete_entity(iri)
        if result:
            logger.info("Deleted entity %s", iri)
        return result

    def get_entity_versions(
        self, iri: str, *, limit: int = 1000, offset: int = 0
    ) -> List[Dict[str, Any]]:
        return self.repo.list_versions_by_entity(iri, limit=limit, offset=offset)

    def count_entity_versions(self, iri: str) -> int:
        return self.repo.count_versions_by_entity(iri)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_or_create_author(self, author_id: str) -> Dict[str, Any]:
        author = self.repo.get_author(author_id)
        if author:
            return author
        slug = author_id.split(":", 1)[1] if ":" in author_id else author_id
        author = {"id": author_id, "slug": slug}
        self.repo.put_author(author_id, author)
        return author


def _version_meta(
    iri: str, version_number: int, author: Dict[str, Any],
    change_description: str, created_at: datetime
) -> Dict[str, Any]:
    """The ``jawafdehi:version`` provenance block embedded in the document."""
    return {
        "entity_iri": iri,
        "version_number": version_number,
        "author": author,
        "change_description": change_description,
        "created_at": created_at.isoformat(),
    }
