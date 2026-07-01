"""NES storage models — schema.org JSON-LD keyed by @id IRI.

CLEAN-SLATE REMODEL (2026-06-28): NES no longer stores rigid per-type Pydantic
documents. The canonical stored form is a raw **schema.org JSON-LD** document in
the ``data`` JSONB column, keyed by the schema.org ``@id`` **IRI**
(``https://jawafdehi.org/entity/<prefix>/<slug>``) — the platform join key (see
``jawafdehi_shared.entities.ids`` + think-big/nes-schemaorg-remodel-plan.md).

There is no legacy ``entity:<prefix>/<slug>`` id, no dual-accept, no backfill —
stores are assumed empty and data is created fresh as JSON-LD.

Promoted columns are derived from the JSON-LD on write (see
``entities.persistence``):
- ``iri`` — the canonical ``@id`` (primary key / join key),
- ``entity_type`` — the schema.org ``@type`` (string; a list @type is joined
  with ``,`` for the promoted column, the full value lives in ``data``),
- ``prefix`` / ``slug`` — parsed from the IRI for routing + prefix listing,
- ``version`` / ``created_at`` / ``updated_at`` — provenance.

MANAGED-TABLE NOTE (mirrors NGM): every model pins ``Meta.db_table``. ``managed``
is left True so the migration is a faithful, reviewable record AND the test DB
(sqlite) can CREATE these tables. In the shared production ``nes`` DB the
SQLAlchemy/asyncpg side is the table authority, so the migration is applied with
``--fake`` there; the db_table pins keep both paths mapped to the same tables.
"""

from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.utils import timezone


class StoredEntity(models.Model):
    """One published entity as a schema.org JSON-LD document.

    ``data`` is the full JSON-LD (``@context``/``@type``/``@id``/schema.org
    props/``jawafdehi:`` extensions). The promoted columns are derived from it on
    write and exist only for filtering/routing.
    """

    iri = models.TextField(primary_key=True)  # @id, e.g. https://jawafdehi.org/entity/person/ram-bahadur
    entity_type = models.TextField(db_index=True)  # @type
    prefix = models.TextField(db_index=True)  # parsed from the IRI (routing/listing)
    slug = models.TextField(db_index=True)  # parsed from the IRI (routing)
    data = models.JSONField()  # the schema.org JSON-LD document
    version = models.IntegerField(default=1)
    # The repository (persistence.EntityRepository) sets these explicitly to carry
    # publish-time provenance (created_at is preserved across re-publishes), so
    # auto_now_add/auto_now would be WRONG — they'd clobber the explicit value.
    # default=timezone.now only supplies a value when one is NOT passed (direct
    # ORM creates, tests), satisfying NOT-NULL without overriding the repo.
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "entities"
        indexes = [
            models.Index(fields=["entity_type"], name="ix_entities_type"),
            # GIN index on the JSONB document so containment filters
            # (``data__keywords__contains=...``) hit an index instead of a full
            # scan. Declared here so it lives in the model state, but it is a
            # Postgres-only access method: the 0001 migration splits it with
            # SeparateDatabaseAndState so the actual ``CREATE INDEX ... USING
            # gin`` runs only on PostgreSQL and is a no-op on sqlite (the DB-less
            # test / local fallback), where GIN does not exist.
            GinIndex(fields=["data"], name="ix_entities_data_gin"),
        ]

    def __str__(self) -> str:
        return self.iri


class StoredVersion(models.Model):
    """An append-only version snapshot of an entity's JSON-LD document."""

    id = models.TextField(primary_key=True)  # version:<iri>:<n>
    subject_iri = models.TextField(db_index=True)  # the entity @id this versions
    version_number = models.IntegerField()
    author_id = models.TextField()
    data = models.JSONField()  # the JSON-LD snapshot at this version
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "versions"
        indexes = [
            models.Index(
                fields=["subject_iri", "version_number"], name="ix_versions_subject"
            ),
        ]

    def __str__(self) -> str:
        return self.id


class StoredAuthor(models.Model):
    """An author document. Keyed by author id (``oidc:<sub>`` / ``author:<slug>``)."""

    id = models.TextField(primary_key=True)
    data = models.JSONField()

    class Meta:
        db_table = "authors"

    def __str__(self) -> str:
        return self.id


class HeldEntity(models.Model):
    """A bulk-ingest record HELD by the >=2-source gate (staged, not published).

    Keyed by the candidate entity IRI; idempotent upsert so a later "source #2
    arrived" pass can re-evaluate it.
    """

    iri = models.TextField(primary_key=True)
    entity_data = models.JSONField()
    sources = models.JSONField()
    reason = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "held_entities"

    def __str__(self) -> str:
        return self.iri
