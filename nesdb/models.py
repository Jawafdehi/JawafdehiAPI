"""Django models for derived NES Postgres tables.

NesEntity stores generic indexed fields plus the full entity JSON in raw_payload.
NesEntityName stores concatenated name_en / name_ne per kind for searchability.
NesRelationship stores generic indexed fields plus the full relationship JSON.
NesSyncState tracks the git commit watermark for incremental imports.

The on-disk source of truth is the nes-db JSON file database.  These tables
are a query-optimised read replica.
"""

from django.db import models


class NesSyncState(models.Model):
    """Watermark record tracking the last synced git commit.

    A single row (pk=1) that is atomically updated inside the same transaction
    as the data mutations so the watermark is consistent with the table state.
    """

    last_commit_hash = models.CharField(
        max_length=64,
        help_text="SHA of the last synced commit in the nes-db repository.",
    )
    last_sync_at = models.DateTimeField(
        auto_now=True,
        help_text="Timestamp of the most recent successful sync.",
    )
    entities_upserted = models.IntegerField(default=0)
    entities_deleted = models.IntegerField(default=0)
    relationships_upserted = models.IntegerField(default=0)
    relationships_deleted = models.IntegerField(default=0)
    error_message = models.TextField(
        blank=True,
        default="",
        help_text="Error details if the last sync failed.",
    )

    class Meta:
        verbose_name = "NES Sync State"
        verbose_name_plural = "NES Sync States"

    def __str__(self):
        return f"SyncState(commit={self.last_commit_hash[:8]}…, at={self.last_sync_at})"


class NesEntity(models.Model):
    """A Nepal Entity Service entity stored in Postgres.

    Generic indexed columns (entity_id, slug, entity_prefix, tags) are
    extracted from the NES payload.  The full entity JSON is stored in
    ``raw_payload`` — prefix-specific fields live there.
    """

    entity_id = models.CharField(
        max_length=512,
        unique=True,
        help_text="Full computed entity ID, e.g. 'entity:person/nepal_govt/john-doe'.",
    )
    slug = models.CharField(
        max_length=100,
        help_text="URL-friendly identifier for the entity.",
    )
    entity_prefix = models.CharField(
        max_length=512,
        help_text="Slash-joined classification prefix, e.g. 'person/nepal_govt'.",
    )
    tags = models.JSONField(
        null=True,
        blank=True,
        help_text="Tags as a list of strings.",
    )
    version_summary = models.JSONField(
        help_text="Latest version summary (VersionSummary as dict).",
    )
    created_at = models.DateTimeField(
        help_text="Timestamp from the NES entity record.",
    )
    raw_payload = models.JSONField(
        help_text="Full NES entity JSON as stored in nes-db.",
    )
    last_modified_at = models.DateTimeField(
        auto_now=True,
        help_text="When this row was last modified in Postgres.",
    )

    class Meta:
        ordering = ["entity_id"]
        indexes = [
            models.Index(fields=["entity_prefix"], name="nesdb_entity_prefix_idx"),
            models.Index(fields=["slug"], name="nesdb_entity_slug_idx"),
        ]
        verbose_name = "NES Entity"
        verbose_name_plural = "NES Entities"

    def __str__(self):
        return self.entity_id


class NesEntityName(models.Model):
    """A name attached to a NesEntity.

    name_en / name_ne hold the concatenated English and Nepali name parts.
    At least one row per entity will have ``kind='PRIMARY'``.
    """

    class NameKind(models.TextChoices):
        PRIMARY = "PRIMARY", "Primary"
        ALIAS = "ALIAS", "Alias"
        ALTERNATE = "ALTERNATE", "Alternate"
        BIRTH = "BIRTH_NAME", "Birth Name"

    entity = models.ForeignKey(
        NesEntity,
        on_delete=models.CASCADE,
        related_name="names",
        db_index=False,
        help_text="The entity this name belongs to.",
    )
    kind = models.CharField(
        max_length=20,
        choices=NameKind.choices,
        help_text="Type of name.",
    )
    name_en = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="Concatenated English/romanised name.",
        # null=True intentional — conditional indexes (nesdb_name_en_idx) exclude
        # NULLs so sparse language columns don't bloat the index with empty rows.
    )
    name_ne = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="Concatenated Nepali (Devanagari) name.",
        # null=True intentional — same rationale as name_en above.
    )

    class Meta:
        ordering = ["entity", "pk"]
        indexes = [
            models.Index(fields=["entity", "kind"], name="nesdb_name_entity_kind_idx"),
            models.Index(
                fields=["name_en"],
                name="nesdb_name_en_idx",
                condition=models.Q(name_en__isnull=False),
            ),
            models.Index(
                fields=["name_ne"],
                name="nesdb_name_ne_idx",
                condition=models.Q(name_ne__isnull=False),
            ),
        ]
        verbose_name = "NES Entity Name"
        verbose_name_plural = "NES Entity Names"

    def __str__(self):
        primary = self.name_en or self.name_ne or ""
        return f"{self.kind}: {primary}"


class NesRelationship(models.Model):
    """A relationship between two NES entities.

    Generic indexed columns are extracted from the NES payload.  The full
    relationship JSON is stored in ``raw_payload`` — type-specific fields
    live there.
    """

    relationship_id = models.CharField(
        max_length=1024,
        unique=True,
        help_text="Full computed relationship ID.",
    )
    source_entity_id = models.CharField(
        max_length=512,
        help_text="Entity ID of the relationship source.",
    )
    target_entity_id = models.CharField(
        max_length=512,
        help_text="Entity ID of the relationship target.",
    )
    type = models.CharField(
        max_length=64,
        help_text="Relationship type, e.g. 'EMPLOYED_BY', 'MEMBER_OF'.",
    )
    raw_payload = models.JSONField(
        help_text="Full NES relationship JSON as stored in nes-db.",
    )
    last_modified_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["relationship_id"]
        indexes = [
            models.Index(fields=["source_entity_id"], name="nesdb_rel_source_idx"),
            models.Index(fields=["target_entity_id"], name="nesdb_rel_target_idx"),
            models.Index(fields=["type"], name="nesdb_rel_type_idx"),
        ]
        verbose_name = "NES Relationship"
        verbose_name_plural = "NES Relationships"

    def __str__(self):
        return self.relationship_id
