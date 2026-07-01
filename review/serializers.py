from rest_framework import serializers

from .models import CaseReview, ReviewConfig


class CaseReviewListSerializer(serializers.ModelSerializer):
    overall_score = serializers.SerializerMethodField()
    disposition = serializers.SerializerMethodField()

    class Meta:
        model = CaseReview
        fields = [
            "id",
            "slug",
            "status",
            "stage",
            "case_title",
            "case_state",
            "source_count",
            "sources_converted",
            "overall_score",
            "disposition",
            "case_type",
            "created_at",
            "completed_at",
            "duration_seconds",
        ]

    def get_overall_score(self, obj):
        return (obj.result or {}).get("overall_score") if obj.result else None

    def get_disposition(self, obj):
        return (obj.result or {}).get("disposition") if obj.result else None


class CaseReviewDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = CaseReview
        fields = [
            "id",
            "slug",
            "status",
            "stage",
            "error",
            "case_title",
            "case_state",
            "case_type",
            "source_count",
            "sources_converted",
            "result",
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
            "duration_seconds",
        ]


class ReviewConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewConfig
        fields = ["pass_threshold", "revise_threshold", "llm_samples", "updated_at"]
        read_only_fields = ["updated_at"]


class SubmitSerializer(serializers.Serializer):
    slug = serializers.CharField(max_length=255)

    def validate_slug(self, value):
        return value.strip().strip("/")


class JobResultSerializer(serializers.Serializer):
    """Payload the poller posts back after processing a claimed job.

    On success, `result` (the full scored result dict) plus the case/source
    metadata is supplied. On failure, `error` is supplied instead.
    """

    status = serializers.ChoiceField(choices=["done", "failed"])
    error = serializers.CharField(required=False, allow_blank=True, default="")

    case_title = serializers.CharField(required=False, allow_blank=True, default="")
    case_state = serializers.CharField(required=False, allow_blank=True, default="")
    case_type = serializers.CharField(required=False, allow_blank=True, default="")
    source_count = serializers.IntegerField(required=False, default=0)
    sources_converted = serializers.IntegerField(required=False, default=0)
    result = serializers.JSONField(required=False, allow_null=True, default=None)
    duration_seconds = serializers.FloatField(
        required=False, allow_null=True, default=None
    )

    def validate(self, attrs):
        if attrs["status"] == "done" and not attrs.get("result"):
            raise serializers.ValidationError(
                {"result": "result is required when status is 'done'."}
            )
        if attrs["status"] == "failed" and not attrs.get("error"):
            raise serializers.ValidationError(
                {"error": "error is required when status is 'failed'."}
            )
        return attrs
