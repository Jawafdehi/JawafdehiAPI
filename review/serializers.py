from rest_framework import serializers

from jawafdehi_shared.entities.ids import (
    MAX_IRI_LENGTH,
    canonicalize_courtcase_iri,
    is_valid_case_iri,
    is_valid_courtcase_iri,
    parse_case_iri,
)

from .models import CaseReview, ReviewConfig


def _result_dict(result):
    """Return ``result`` iff it's a dict, else ``{}``.

    ``CaseReview.result`` is a JSONField, so a malformed/legacy row could hold a
    list or scalar. All the derived read fields go through this so a bad shape
    degrades to "no data" instead of 500-ing the review list/detail endpoints.
    """
    return result if isinstance(result, dict) else {}


def _reviewers_from_result(result):
    """Project a review's per-tier LLM usage into the reviewer-chip shape.

    The runner records token usage per ``(provider, tier, model)`` bucket in
    ``result["token_usage"]["by_provider"]`` (see llm.usage.UsageAccumulator).
    The frontend renders reviewer attribution from a ``reviewers`` list of
    ``{tier, provider, model, calls}`` — a single review typically lists more
    than one entry because gate rules use the premium tier while routine
    rules/narrative use the cheap tier. Return the projected list, or ``None``
    when the review hasn't produced usage yet (pending/failed runs).
    """
    # ``result`` is a JSONField, so guard every level: malformed/legacy stored
    # data (a non-dict result, a scalar token_usage, a non-list by_provider)
    # must yield None, never crash the serializer.
    token_usage = _result_dict(result).get("token_usage")
    if not isinstance(token_usage, dict):
        return None
    buckets = token_usage.get("by_provider")
    if not isinstance(buckets, list):
        return None
    reviewers = [
        {
            "tier": b.get("tier", ""),
            "provider": b.get("provider", ""),
            "model": b.get("model", ""),
            "calls": b.get("calls", 0),
        }
        for b in buckets
        if isinstance(b, dict)
    ]
    return reviewers or None


class CaseReviewListSerializer(serializers.ModelSerializer):
    overall_score = serializers.SerializerMethodField()
    disposition = serializers.SerializerMethodField()
    reviewers = serializers.SerializerMethodField()
    # Derived (read-only) from the linked case: the FE builds the case URL/link
    # from it. It is the model ``slug`` property, not a stored column.
    slug = serializers.ReadOnlyField()

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
            "reviewers",
            "case_type",
            "created_at",
            "completed_at",
            "duration_seconds",
        ]

    def get_overall_score(self, obj):
        return _result_dict(obj.result).get("overall_score")

    def get_disposition(self, obj):
        return _result_dict(obj.result).get("disposition")

    def get_reviewers(self, obj):
        return _reviewers_from_result(obj.result)


class CaseReviewDetailSerializer(serializers.ModelSerializer):
    reviewers = serializers.SerializerMethodField()
    # Derived (read-only) from the linked case — see CaseReviewListSerializer.
    slug = serializers.ReadOnlyField()

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
            "reviewers",
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
            "duration_seconds",
        ]

    def get_reviewers(self, obj):
        return _reviewers_from_result(obj.result)


class ReviewConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = ReviewConfig
        fields = ["pass_threshold", "revise_threshold", "llm_samples", "updated_at"]
        read_only_fields = ["updated_at"]


def _require_case(identifier, *, source_label):
    """Return the Case named by ``identifier`` (its slug); else raise a 400.

    ``source_label`` is the field the caseworker actually submitted (``iri`` or
    ``slug``) so the error attaches to it in the DRF response. Returns the Case
    OBJECT — reviews key on the stable case PK, not the mutable slug.
    """
    from cases.models import Case

    case = Case.objects.filter(slug=identifier).first()
    if case is None:
        raise serializers.ValidationError(
            {source_label: f"No Jawafdehi case found for slug '{identifier}'."}
        )
    return case


def _resolve_iri_to_case(iri):
    """Resolve a canonical @id IRI to the Jawafdehi Case it names.

    Accepts either form (host is not anchored — the DB lookup is the authority):

      * a Jawafdehi case IRI ``https://<base>/case/<slug>`` — the slug is the
        case's external id, used directly.
      * a court-case IRI ``https://<base>/courtcase/<court>/<case_number>`` —
        resolved to the single Jawafdehi case that references it (via the
        ``CaseCourtCaseReference`` join). Ambiguous (>1 case) or unreferenced
        court cases are rejected with a 400.

    Returns the Case OBJECT on success (reviews key on the case PK).
    """
    from cases.models import Case

    if is_valid_case_iri(iri, any_host=True):
        return _require_case(parse_case_iri(iri).slug, source_label="iri")

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
        return Case.objects.get(slug=slugs[0])

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
    """Resolve a submitted identifier to the Jawafdehi Case it names.

    Caseworkers submit an ``iri`` — either the Jawafdehi case IRI or a
    court-case IRI (see :func:`_resolve_iri_to_case`). A bare ``slug`` (an exact
    canonical case slug) is also accepted for the internal re-run / regrade
    paths, which already hold the resolved slug. Exactly one must be given.
    ``validated_data['case']`` is the resolved Case object on success (reviews
    key on the stable case PK, not the mutable slug).
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
        attrs.pop("slug", None)
        attrs["case"] = (
            _resolve_iri_to_case(iri)
            if iri
            else _require_case(slug, source_label="slug")
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
