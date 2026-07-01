from rest_framework import serializers

from .models import Job


class JobSerializer(serializers.ModelSerializer):
    """Read serializer for the queue dashboard (GET /api/jobs)."""

    class Meta:
        model = Job
        fields = [
            "id",
            "kind",
            "status",
            "priority",
            "stage",
            "dedup_key",
            "attempts",
            "max_attempts",
            "error",
            "available_at",
            "lease_expires_at",
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
            "duration_seconds",
        ]


class JobClaimSerializer(serializers.Serializer):
    """Body of POST /api/jobs/claim — the kinds a consumer will accept."""

    kinds = serializers.ListField(
        child=serializers.CharField(max_length=64),
        allow_empty=False,
    )


class JobEnqueueSerializer(serializers.Serializer):
    """Body of POST /api/jobs — enqueue a unit of work."""

    kind = serializers.CharField(max_length=64)
    payload = serializers.JSONField(required=False, default=dict)
    dedup_key = serializers.CharField(
        max_length=255, required=False, allow_null=True, default=None
    )
    priority = serializers.IntegerField(required=False, default=100)


class JobStageSerializer(serializers.Serializer):
    stage = serializers.CharField(max_length=64, allow_blank=True, default="")


class JobResultSerializer(serializers.Serializer):
    """Body of POST /api/jobs/{id}/result — the consumer's outcome.

    ``result`` carries the handler's output on success; ``error`` on failure.
    ``retryable`` (failure only) asks the queue to re-queue with backoff rather
    than fail terminally.
    """

    status = serializers.ChoiceField(choices=["done", "failed"])
    result = serializers.JSONField(required=False, allow_null=True, default=None)
    error = serializers.CharField(required=False, allow_blank=True, default="")
    retryable = serializers.BooleanField(required=False, default=False)
    duration_seconds = serializers.FloatField(
        required=False, allow_null=True, default=None
    )

    def validate(self, attrs):
        if attrs["status"] == "done" and attrs.get("result") is None:
            raise serializers.ValidationError(
                {"result": "result is required when status is 'done'."}
            )
        if attrs["status"] == "failed" and not attrs.get("error"):
            raise serializers.ValidationError(
                {"error": "error is required when status is 'failed'."}
            )
        return attrs
