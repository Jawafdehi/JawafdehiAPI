from rest_framework import serializers

from .models import (
    AccessLevel,
    KnowledgeChunk,
    KnowledgeCollection,
    KnowledgeEmbedding,
    KnowledgeSource,
    has_public_citation_metadata,
)


class KnowledgeCollectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeCollection
        fields = [
            "id",
            "name",
            "display_name",
            "description",
            "access_level",
            "is_active",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]


class KnowledgeSourceSerializer(serializers.ModelSerializer):
    collection_name = serializers.CharField(source="collection.name", read_only=True)
    chunk_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = KnowledgeSource
        fields = [
            "id",
            "collection",
            "collection_name",
            "title",
            "source_type",
            "source_url",
            "chunk_count",
            "storage_path",
            "checksum",
            "metadata",
            "access_level",
            "is_active",
            "owner",
            "allowed_users",
            "allowed_groups",
            "case",
            "document_source",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "collection_name", "created_at", "updated_at"]

    def validate(self, attrs):
        access_level = attrs.get(
            "access_level",
            getattr(self.instance, "access_level", AccessLevel.PRIVATE),
        )
        source_url = attrs.get("source_url", getattr(self.instance, "source_url", ""))
        metadata = attrs.get("metadata", getattr(self.instance, "metadata", {}))
        document_source = attrs.get(
            "document_source", getattr(self.instance, "document_source", None)
        )
        if access_level == AccessLevel.PUBLIC and not (
            source_url or document_source or has_public_citation_metadata(metadata)
        ):
            raise serializers.ValidationError(
                {
                    "source_url": (
                        "Public knowledge sources need a source URL, linked document "
                        "source, or metadata.public_citation for citations."
                    )
                }
            )
        return attrs


class KnowledgeChunkSerializer(serializers.ModelSerializer):
    source_title = serializers.CharField(source="source.title", read_only=True)
    collection_name = serializers.CharField(
        source="source.collection.name", read_only=True
    )

    class Meta:
        model = KnowledgeChunk
        fields = [
            "id",
            "source",
            "source_title",
            "collection_name",
            "text",
            "chunk_index",
            "page_start",
            "page_end",
            "section_title",
            "table_title",
            "metadata",
            "content_hash",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "source_title",
            "collection_name",
            "created_at",
            "updated_at",
        ]


class KnowledgeEmbeddingSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeEmbedding
        fields = [
            "id",
            "chunk",
            "embedding_model",
            "dimensions",
            "embedding",
            "metadata",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]
