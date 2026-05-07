from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.db import models
from pgvector.django import VectorField


class AccessLevel(models.TextChoices):
    PRIVATE = "private", "Private"
    PUBLIC = "public", "Public"


def has_public_citation_metadata(metadata: dict) -> bool:
    citation = metadata.get("public_citation") if isinstance(metadata, dict) else None
    if not isinstance(citation, dict):
        return False
    return bool(
        citation.get("title") or citation.get("url") or citation.get("identifier")
    )


class KnowledgeCollection(models.Model):
    """A logical bucket of searchable knowledge artifacts."""

    name = models.SlugField(max_length=120, unique=True)
    display_name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    access_level = models.CharField(
        max_length=20,
        choices=AccessLevel.choices,
        default=AccessLevel.PRIVATE,
        db_index=True,
    )
    is_active = models.BooleanField(default=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return self.display_name or self.name

    class Meta:
        ordering = ["name"]


class KnowledgeSource(models.Model):
    """A source document or artifact that has been chunked for retrieval."""

    collection = models.ForeignKey(
        KnowledgeCollection, on_delete=models.CASCADE, related_name="sources"
    )
    title = models.CharField(max_length=300)
    source_type = models.CharField(max_length=80, default="document")
    source_url = models.URLField(max_length=1000, blank=True)
    storage_path = models.CharField(max_length=1000, blank=True)
    checksum = models.CharField(max_length=128, blank=True, db_index=True)
    metadata = models.JSONField(default=dict, blank=True)
    access_level = models.CharField(
        max_length=20,
        choices=AccessLevel.choices,
        default=AccessLevel.PRIVATE,
        db_index=True,
    )
    is_active = models.BooleanField(default=True, db_index=True)
    owner = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="owned_knowledge_sources",
    )
    allowed_users = models.ManyToManyField(
        User, blank=True, related_name="shared_knowledge_sources"
    )
    allowed_groups = models.ManyToManyField(
        Group, blank=True, related_name="knowledge_sources"
    )
    case = models.ForeignKey(
        "cases.Case",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="knowledge_sources",
    )
    document_source = models.ForeignKey(
        "cases.DocumentSource",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="knowledge_sources",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self) -> None:
        errors = {}
        if self.access_level == AccessLevel.PUBLIC:
            has_citation_target = bool(
                self.source_url
                or self.document_source_id
                or has_public_citation_metadata(self.metadata)
            )
            if not has_citation_target:
                errors["source_url"] = (
                    "Public knowledge sources need a source URL, linked document "
                    "source, or metadata.public_citation for citations."
                )
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return self.title

    class Meta:
        ordering = ["collection__name", "title"]
        indexes = [
            models.Index(fields=["collection", "access_level", "is_active"]),
            models.Index(fields=["checksum"]),
        ]


class KnowledgeChunk(models.Model):
    """A bounded text chunk with citation metadata."""

    source = models.ForeignKey(
        KnowledgeSource, on_delete=models.CASCADE, related_name="chunks"
    )
    text = models.TextField()
    chunk_index = models.PositiveIntegerField()
    page_start = models.PositiveIntegerField(null=True, blank=True)
    page_end = models.PositiveIntegerField(null=True, blank=True)
    section_title = models.CharField(max_length=300, blank=True)
    table_title = models.CharField(max_length=300, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    content_hash = models.CharField(max_length=64, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self) -> None:
        errors = {}
        if self.page_start and self.page_end and self.page_end < self.page_start:
            errors["page_end"] = "Page end cannot be before page start."
        if not self.text.strip():
            errors["text"] = "Knowledge chunks cannot be empty."
        if errors:
            raise ValidationError(errors)

    def __str__(self) -> str:
        return f"{self.source}: chunk {self.chunk_index}"

    class Meta:
        ordering = ["source_id", "chunk_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["source", "chunk_index"],
                name="knowledge_unique_source_chunk_index",
            ),
            models.UniqueConstraint(
                fields=["source", "content_hash"],
                name="knowledge_unique_source_content_hash",
            ),
        ]
        indexes = [
            models.Index(fields=["source", "chunk_index"]),
            models.Index(fields=["content_hash"]),
        ]


class KnowledgeEmbedding(models.Model):
    """
    Embedding row for production/vector search.

    The current model stores vectors as JSON so SQLite/dev/test stays portable.
    Postgres deployments can add pgvector-backed indexes against this table in a
    database-specific migration without changing the retrieval contract.
    """

    chunk = models.ForeignKey(
        KnowledgeChunk, on_delete=models.CASCADE, related_name="embeddings"
    )
    embedding_model = models.CharField(max_length=255)
    dimensions = models.PositiveIntegerField(default=0)
    vector = VectorField(dimensions=None, null=True, blank=True)
    embedding = models.JSONField(default=list, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self) -> None:
        errors = {}
        if self.embedding:
            if not isinstance(self.embedding, list):
                errors["embedding"] = "Embedding must be a list of numbers."
            elif not all(isinstance(value, (int, float)) for value in self.embedding):
                errors["embedding"] = "Embedding must contain only numbers."
            elif self.dimensions and len(self.embedding) != self.dimensions:
                errors["dimensions"] = "Dimensions must match embedding length."
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.embedding and not self.dimensions:
            self.dimensions = len(self.embedding)
        if self.embedding and self.vector is None:
            self.vector = self.embedding
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.embedding_model}: {self.chunk_id}"

    class Meta:
        ordering = ["chunk_id", "embedding_model"]
        constraints = [
            models.UniqueConstraint(
                fields=["chunk", "embedding_model"],
                name="knowledge_unique_chunk_embedding_model",
            )
        ]
