from urllib.parse import urlparse

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


def slug_from_input(value):
    """Normalize a slug field that may be a bare slug or a full case URL.

    Accepts "alpha-case", "https://jawafdehi.org/case/alpha-case" (with or
    without scheme, query string, or trailing slash). Returns the slug — the
    path segment following "/case/" when present, otherwise the last segment.
    """
    value = (value or "").strip()
    if not value:
        return ""
    path = urlparse(value).path
    segments = [s for s in path.split("/") if s]
    if not segments:
        return ""
    if "case" in segments:
        idx = segments.index("case")
        if idx + 1 < len(segments):
            return segments[idx + 1]
    return segments[-1]


class SubmitSerializer(serializers.Serializer):
    """Submit a review by case slug OR by court case number (exactly one).

    The slug may be given as a bare slug or a full case URL (the slug is
    extracted). The court case number is the "court:number" ref stored on the
    case (e.g. "special:081-CR-0079"); the view resolves either form to a
    concrete case.
    """

    slug = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )
    court_case_number = serializers.CharField(
        max_length=255, required=False, allow_blank=True, default=""
    )

    def validate(self, attrs):
        slug = slug_from_input(attrs.get("slug"))
        court_case_number = (attrs.get("court_case_number") or "").strip()
        if not slug and not court_case_number:
            raise serializers.ValidationError(
                "Provide either 'slug' or 'court_case_number'."
            )
        if slug and court_case_number:
            raise serializers.ValidationError(
                "Provide only one of 'slug' or 'court_case_number', not both."
            )
        attrs["slug"] = slug
        attrs["court_case_number"] = court_case_number
        return attrs


class SourceMarkdownSerializer(serializers.Serializer):
    """Markdown the poller attaches to a DocumentSource (MARKDOWN-role url)."""

    markdown = serializers.CharField(trim_whitespace=False)
    overwrite = serializers.BooleanField(required=False, default=False)


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
