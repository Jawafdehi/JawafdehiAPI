from rest_framework import serializers


class PublicChatHistoryItemSerializer(serializers.Serializer):
    role = serializers.ChoiceField(choices=["user", "assistant"])
    content = serializers.CharField(allow_blank=False, trim_whitespace=True)


class PublicChatRequestSerializer(serializers.Serializer):
    question = serializers.CharField(allow_blank=False, trim_whitespace=True)
    session_id = serializers.CharField(required=False, allow_blank=True, max_length=200)
    history = PublicChatHistoryItemSerializer(many=True, required=False)
    language = serializers.CharField(required=False, allow_blank=True, max_length=20)


class PublicChatSourceSerializer(serializers.Serializer):
    source_ref = serializers.CharField(required=False, allow_blank=True)
    title = serializers.CharField()
    url = serializers.CharField(allow_blank=True)
    type = serializers.CharField()
    snippet = serializers.CharField(required=False, allow_blank=True)
    source_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    document_id = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    chunk_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    page_start = serializers.IntegerField(required=False, allow_null=True)
    page_end = serializers.IntegerField(required=False, allow_null=True)
    score = serializers.FloatField(required=False, allow_null=True)
    retrieval_mode = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    citation_identifier = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    citation_publisher = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    citation_publication_date = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )


class PublicChatRelatedCaseSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    title = serializers.CharField()
    url = serializers.CharField()
    slug = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    case_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    short_description = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )


class PublicChatResponseSerializer(serializers.Serializer):
    answer_text = serializers.CharField()
    session_id = serializers.CharField(required=False)
    sources = PublicChatSourceSerializer(many=True)
    related_cases = PublicChatRelatedCaseSerializer(many=True)
    follow_up_questions = serializers.ListField(child=serializers.CharField())


class PublicEvidenceEntitySerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False, allow_null=True)
    nes_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    display_name = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    type = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    related_cases = serializers.ListField(
        child=serializers.DictField(),
        required=False,
    )


class PublicEvidenceCaseSerializer(serializers.Serializer):
    id = serializers.IntegerField(min_value=1)
    case_id = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    slug = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    case_type = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    state = serializers.ChoiceField(choices=["PUBLISHED"])
    title = serializers.CharField(allow_blank=True)
    short_description = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    description = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    key_allegations = serializers.ListField(
        child=serializers.CharField(allow_blank=True),
        required=False,
    )
    tags = serializers.ListField(
        child=serializers.CharField(allow_blank=True),
        required=False,
    )
    entities = PublicEvidenceEntitySerializer(many=True, required=False)
    case_start_date = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    case_end_date = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    created_at = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    updated_at = serializers.CharField(
        required=False, allow_blank=True, allow_null=True
    )
    evidence = serializers.ListField(child=serializers.DictField(), required=False)
