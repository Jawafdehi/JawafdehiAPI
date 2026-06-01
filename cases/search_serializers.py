"""OpenAPI serializers for unified archive search."""

from drf_spectacular.utils import PolymorphicProxySerializer, extend_schema_field
from rest_framework import serializers


class SearchFacetItemSerializer(serializers.Serializer):
    name = serializers.CharField()
    display_name = serializers.CharField()
    count = serializers.IntegerField(min_value=0)


class SearchCountsSerializer(serializers.Serializer):
    all = serializers.IntegerField(min_value=0)
    cases = serializers.IntegerField(min_value=0)
    entities = serializers.IntegerField(min_value=0)
    documents = serializers.IntegerField(min_value=0)


class SearchResultEntityPreviewSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    display_name = serializers.CharField(allow_null=True)
    nes_id = serializers.CharField(allow_null=True)
    relationship_type = serializers.CharField(required=False)


class SearchResultSerializer(serializers.Serializer):
    result_type = serializers.CharField()
    id = serializers.IntegerField()
    title = serializers.CharField()
    description = serializers.CharField()
    url = serializers.CharField()
    api_url = serializers.CharField()
    matched_fields = serializers.ListField(child=serializers.CharField())
    score = serializers.IntegerField()


class SearchCaseResultSerializer(SearchResultSerializer):
    result_type = serializers.ChoiceField(choices=["case"])
    slug = serializers.CharField()
    state = serializers.CharField()
    case_type = serializers.CharField()
    date = serializers.DateField(allow_null=True)
    tags = serializers.ListField(child=serializers.CharField())
    entities = SearchResultEntityPreviewSerializer(many=True)


class SearchEntityResultSerializer(SearchResultSerializer):
    result_type = serializers.ChoiceField(choices=["entity"])
    entity_type = serializers.CharField()
    nes_id = serializers.CharField(allow_null=True)
    role_counts = serializers.DictField(child=serializers.IntegerField(min_value=0))
    related_case_count = serializers.IntegerField(min_value=0)


class SearchDocumentResultSerializer(SearchResultSerializer):
    result_type = serializers.ChoiceField(choices=["document"])
    source_id = serializers.CharField()
    source_type = serializers.CharField(allow_null=True)
    related_entities = SearchResultEntityPreviewSerializer(many=True)


class SearchFacetsSerializer(serializers.Serializer):
    entity_type = SearchFacetItemSerializer(many=True)
    role = SearchFacetItemSerializer(many=True)
    case_type = SearchFacetItemSerializer(many=True)
    tags = SearchFacetItemSerializer(many=True)


class SearchResponseSerializer(serializers.Serializer):
    query = serializers.CharField()
    page = serializers.IntegerField(min_value=1)
    page_size = serializers.IntegerField(min_value=1, max_value=50)
    count = serializers.IntegerField(min_value=0)
    counts = SearchCountsSerializer()
    facets = SearchFacetsSerializer()
    results = serializers.SerializerMethodField()

    @extend_schema_field(
        PolymorphicProxySerializer(
            component_name="ArchiveSearchResult",
            serializers=[
                SearchCaseResultSerializer,
                SearchEntityResultSerializer,
                SearchDocumentResultSerializer,
            ],
            resource_type_field_name="result_type",
            many=True,
        )
    )
    def get_results(self, obj):
        return obj["results"]
