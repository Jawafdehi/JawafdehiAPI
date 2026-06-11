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
