from rest_framework import serializers

from jawafdehi_shared.entities.ids import (
    MAX_IRI_LENGTH,
    canonicalize_courtcase_iri,
    is_valid_case_iri,
    is_valid_courtcase_iri,
    parse_case_iri,
)

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


def _require_case_slug(slug, *, source_label):
    """Return ``slug`` iff a Case row has it; else raise a 400 naming the input.

    ``source_label`` is the field the caseworker actually submitted (``iri`` or
    ``slug``) so the error attaches to it in the DRF response.
    """
    from cases.models import Case

    if not Case.objects.filter(slug=slug).exists():
        raise serializers.ValidationError(
            {source_label: f"No Jawafdehi case found for slug '{slug}'."}
        )
    return slug


def _resolve_iri_to_slug(iri):
    """Resolve a canonical @id IRI to the slug of the Jawafdehi case it names.

    Accepts either form (host is not anchored — the DB lookup is the authority):

      * a Jawafdehi case IRI ``https://<base>/case/<slug>`` — the slug is the
        case's external id, used directly.
      * a court-case IRI ``https://<base>/courtcase/<court>/<case_number>`` —
        resolved to the single Jawafdehi case that references it (via the
        ``CaseCourtCaseReference`` join). Ambiguous (>1 case) or unreferenced
        court cases are rejected with a 400.
    """
    from cases.models import Case

    if is_valid_case_iri(iri, any_host=True):
        return _require_case_slug(parse_case_iri(iri).slug, source_label="iri")

    if is_valid_courtcase_iri(iri, any_host=True):
        canonical = canonicalize_courtcase_iri(iri)
        # Order by slug (not id): with values_list(...).distinct(), any ORDER BY
        # column is folded into the SELECT DISTINCT, so ordering by the unique id
        # would defeat the de-dup. slug is the projected column, so this stays a
        # distinct-on-slug and gives a deterministic multi-case message.
        slugs = list(
            Case.objects.filter(courtcase_references__courtcase_iri=canonical)
            .order_by("slug")
            .values_list("slug", flat=True)
            .distinct()
        )
        if not slugs:
            raise serializers.ValidationError(
                {"iri": f"No Jawafdehi case references court-case IRI '{canonical}'."}
            )
        if len(slugs) > 1:
            raise serializers.ValidationError(
                {
                    "iri": (
                        f"Court-case IRI '{canonical}' is referenced by multiple cases "
                        f"({', '.join(slugs)}); submit the specific case IRI instead."
                    )
                }
            )
        return slugs[0]

    raise serializers.ValidationError(
        {
            "iri": (
                "Submit a canonical @id IRI: a Jawafdehi case IRI "
                "('https://<base>/case/<slug>') or a court-case IRI "
                "('https://<base>/courtcase/<court>/<case-number>')."
            )
        }
    )


class SubmitSerializer(serializers.Serializer):
    """Resolve a submitted identifier to a canonical Jawafdehi case slug.

    Caseworkers submit an ``iri`` — either the Jawafdehi case IRI or a
    court-case IRI (see :func:`_resolve_iri_to_slug`). A bare ``slug`` (an exact
    canonical case slug) is also accepted for the internal re-run / regrade
    paths, which already hold the resolved slug. Exactly one must be given.
    ``validated_data['slug']`` is always the canonical case slug on success.
    """

    iri = serializers.CharField(
        max_length=MAX_IRI_LENGTH, required=False, allow_blank=True
    )
    slug = serializers.CharField(max_length=255, required=False, allow_blank=True)

    def validate(self, attrs):
        iri = (attrs.get("iri") or "").strip()
        slug = (attrs.get("slug") or "").strip().strip("/")
        if bool(iri) == bool(slug):
            raise serializers.ValidationError(
                "Submit exactly one of 'iri' (a Jawafdehi case or court-case @id IRI) "
                "or 'slug'."
            )
        attrs.pop("iri", None)
        attrs["slug"] = (
            _resolve_iri_to_slug(iri)
            if iri
            else _require_case_slug(slug, source_label="slug")
        )
        return attrs


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
