from rest_framework import serializers


class RelatedCaseSerializer(serializers.Serializer):
    """Minimal case payload embedded in the article API for cross-linking."""

    id = serializers.IntegerField(source="pk")
    title = serializers.CharField()
    slug = serializers.CharField()
