"""Django models for derived NES Postgres tables.

NesEntity mirrors the NES Pydantic Entity model flattened into relational
columns + JSON fields for complex nested structures.  NesEntityName is a
separate table so names can be queried efficiently.  NesRelationship mirrors
the NES Relationship model.  NesSyncState tracks the git commit watermark
for incremental imports.

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
        max_length=40,
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

    The full computed entity ID is stored in ``entity_id`` (e.g.
    ``entity:person/nepal_govt/john-doe``).  ``entity_prefix`` is the
    slash-joined classification path (e.g. ``person/nepal_govt``) and
    ``slug`` is the leaf identifier.

    Complex nested structures (names, version_summary, contacts, etc.) are
    stored as JSON columns — names are duplicated into the NesEntityName
    table for efficient querying.
    """

    entity_id = models.CharField(
        max_length=512,
        unique=True,
        db_index=True,
        help_text="Full computed entity ID, e.g. 'entity:person/nepal_govt/john-doe'.",
    )
    slug = models.CharField(
        max_length=100,
        help_text="URL-friendly identifier for the entity.",
    )
    entity_prefix = models.CharField(
        max_length=512,
        db_index=True,
        help_text="Slash-joined classification prefix, e.g. 'person/nepal_govt'.",
    )
    type = models.CharField(
        max_length=32,
        help_text="Entity type: person, organization, location, or project.",
    )
    sub_type = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="Entity subtype (deprecated in NES, use entity_prefix).",
    )
    version_summary = models.JSONField(
        help_text="Latest version summary (VersionSummary as dict).",
    )
    created_at = models.DateTimeField(
        help_text="Timestamp from the NES entity record.",
    )
    identifiers = models.JSONField(
        null=True,
        blank=True,
        help_text="External identifiers (list of dicts).",
    )
    tags = models.JSONField(
        null=True,
        blank=True,
        help_text="Tags as a list of strings.",
    )
    attributes = models.JSONField(
        null=True,
        blank=True,
        help_text="Additional free-form attributes.",
    )
    contacts = models.JSONField(
        null=True,
        blank=True,
        help_text="Contact information (list of dicts).",
    )
    short_description = models.JSONField(
        null=True,
        blank=True,
        help_text="Brief description as LangText dict.",
    )
    description = models.JSONField(
        null=True,
        blank=True,
        help_text="Detailed description as LangText dict.",
    )
    attributions = models.JSONField(
        null=True,
        blank=True,
        help_text="Source attributions (list of dicts).",
    )
    pictures = models.JSONField(
        null=True,
        blank=True,
        help_text="Entity pictures (list of dicts).",
    )
    last_modified_at = models.DateTimeField(
        auto_now=True,
        help_text="When this row was last modified in Postgres.",
    )

    class Meta:
        ordering = ["entity_id"]
        indexes = [
            models.Index(fields=["type"], name="nesdb_entity_type_idx"),
            models.Index(fields=["entity_prefix"], name="nesdb_entity_prefix_idx"),
            models.Index(fields=["slug"], name="nesdb_entity_slug_idx"),
        ]
        verbose_name = "NES Entity"
        verbose_name_plural = "NES Entities"

    def __str__(self):
        return self.entity_id


class NesEntityName(models.Model):
    """A name attached to a NesEntity.

    Mirrors the NES ``Name`` model.  At least one row per entity will have
    ``kind='PRIMARY'``.  English and Nepali name parts are stored as
    individual columns for queryability.
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
        help_text="The entity this name belongs to.",
    )
    kind = models.CharField(
        max_length=20,
        choices=NameKind.choices,
        help_text="Type of name.",
    )
    en_full = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="English/romanised full name.",
    )
    en_given = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        help_text="English given name.",
    )
    en_middle = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        help_text="English middle name.",
    )
    en_family = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        help_text="English family name.",
    )
    en_prefix = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="English name prefix.",
    )
    en_suffix = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="English name suffix.",
    )
    ne_full = models.CharField(
        max_length=500,
        null=True,
        blank=True,
        help_text="Nepali (Devanagari) full name.",
    )
    ne_given = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        help_text="Nepali given name.",
    )
    ne_middle = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        help_text="Nepali middle name.",
    )
    ne_family = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        help_text="Nepali family name.",
    )
    ne_prefix = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Nepali name prefix.",
    )
    ne_suffix = models.CharField(
        max_length=100,
        null=True,
        blank=True,
        help_text="Nepali name suffix.",
    )

    class Meta:
        ordering = ["entity", "pk"]
        indexes = [
            models.Index(fields=["entity", "kind"], name="nesdb_name_entity_kind_idx"),
        ]
        verbose_name = "NES Entity Name"
        verbose_name_plural = "NES Entity Names"

    def __str__(self):
        primary = self.en_full or self.ne_full or ""
        return f"{self.kind}: {primary}"


class NesRelationship(models.Model):
    """A relationship between two NES entities.

    Mirrors the NES ``Relationship`` model.  ``relationship_id`` is the full
    computed ID (e.g. ``relationship:person/a:org/b:EMPLOYED_BY``).
    """

    relationship_id = models.CharField(
        max_length=1024,
        unique=True,
        db_index=True,
        help_text="Full computed relationship ID.",
    )
    source_entity_id = models.CharField(
        max_length=512,
        db_index=True,
        help_text="Entity ID of the relationship source.",
    )
    target_entity_id = models.CharField(
        max_length=512,
        db_index=True,
        help_text="Entity ID of the relationship target.",
    )
    type = models.CharField(
        max_length=64,
        help_text="Relationship type, e.g. 'EMPLOYED_BY', 'MEMBER_OF'.",
    )
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    attributes = models.JSONField(null=True, blank=True)
    version_summary = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(null=True, blank=True)
    attributions = models.JSONField(null=True, blank=True)
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
