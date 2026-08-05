from rest_framework import serializers

from .models import SUPPORTED_INTENT_TYPES, CaseUpdateProposal


def validate_intent_shape(intent):
    """Shallow shape check on create — enough to reject obviously malformed
    intents early. Deep validation (and the authority on what can be committed)
    happens at apply time in ``case_proposals.apply``."""
    if not isinstance(intent, dict):
        raise serializers.ValidationError("intent must be an object.")
    itype = intent.get("type")
    if itype not in SUPPORTED_INTENT_TYPES:
        raise serializers.ValidationError(
            f"Unknown intent type '{itype}'. Known: {list(SUPPORTED_INTENT_TYPES)}."
        )
    if itype == "append_timeline_entry":
        entry = intent.get("entry")
        if not isinstance(entry, dict) or not entry.get("date") or not entry.get("title"):
            raise serializers.ValidationError(
                "append_timeline_entry requires entry.date and entry.title."
            )
    elif itype == "link_material":
        if not intent.get("material"):
            raise serializers.ValidationError("link_material requires `material`.")
    elif itype == "raw_patch":
        if not isinstance(intent.get("patch"), list) or not intent["patch"]:
            raise serializers.ValidationError("raw_patch requires a non-empty `patch` list.")
    elif itype == "set_entity_outcome":
        # A list, not a single pair: one verdict decides every defendant at once,
        # so the reviewable unit is the whole disposition. Filing one proposal per
        # defendant would invite a reviewer to approve half an acquittal and leave
        # the case internally inconsistent.
        outcomes = intent.get("outcomes")
        if not isinstance(outcomes, list) or not outcomes:
            raise serializers.ValidationError(
                "set_entity_outcome requires a non-empty `outcomes` list."
            )
        for item in outcomes:
            if not isinstance(item, dict) or not item.get("nes_id") or not item.get("outcome"):
                raise serializers.ValidationError(
                    "each set_entity_outcome entry requires `nes_id` and `outcome`."
                )
    return intent


class CaseUpdateProposalSerializer(serializers.ModelSerializer):
    intent = serializers.JSONField()
    # Required on every proposal, bounded to [0, 1].
    confidence = serializers.FloatField(min_value=0.0, max_value=1.0)

    class Meta:
        model = CaseUpdateProposal
        fields = [
            "id",
            "case_slug",
            "case_title",
            "source_kind",
            "intent",
            "confidence",
            "status",
            "source",
            "detected_by",
            "dedup_key",
            "origin_subject",
            "origin_msg_id",
            "subject_refs",
            "reviewer",
            "reviewed_at",
            "review_notes",
            "created_at",
            "updated_at",
        ]
        # Lifecycle fields are set by the approve/reject actions, never on create.
        read_only_fields = [
            "id",
            "status",
            "reviewer",
            "reviewed_at",
            "review_notes",
            "created_at",
            "updated_at",
        ]

    def validate_intent(self, value):
        return validate_intent_shape(value)

    def validate_subject_refs(self, value):
        """Reject anything that is not a list of non-empty strings.

        ``subject_refs`` is a bare ``JSONField``, so without this it accepts a
        scalar, a dict, or a nested list. That is not merely untidy: these are
        ``@id`` IRIs used as the join key between a bus message and our records,
        and a malformed value used to break the approve/reject path outright.

        Enforced here rather than only at publish time because a proposal is
        effectively unfixable once created — the viewset exposes no update, and
        ``dedup_key`` is unique, so the same fact cannot simply be re-filed.
        """
        if not isinstance(value, list):
            raise serializers.ValidationError(
                f"subject_refs must be a list of @id IRIs, got {type(value).__name__}."
            )
        bad = [ref for ref in value if not isinstance(ref, str) or not ref.strip()]
        if bad:
            raise serializers.ValidationError(
                f"subject_refs must contain only non-empty strings; got {bad!r}."
            )
        return value

    def validate_case_slug(self, value):
        """Require a slug the canonical ``@id`` grammar accepts.

        ``case_slug`` is a ``SlugField``, which permits underscores and a leading
        digit; ``build_case_iri`` does not. A proposal whose slug passes the field
        but fails the IRI builder publishes its decision with an EMPTY
        ``subject_refs`` — a message with no join key — and the reject path never
        notices, because it never resolves the Case at all.
        """
        from jawafdehi_shared.entities.ids import build_case_iri

        try:
            build_case_iri(value)
        except Exception as exc:  # noqa: BLE001 - re-raised as a DRF ValidationError
            raise serializers.ValidationError(
                f"case_slug {value!r} is not a valid case @id segment: {exc}"
            ) from None
        return value


class ProposalDecisionSerializer(serializers.Serializer):
    """Request body for approve/reject: an optional review note."""

    notes = serializers.CharField(required=False, allow_blank=True, default="")


class ProposalIntentEditSerializer(serializers.Serializer):
    """Request body for editing a PENDING proposal's proposed change.

    Only ``intent`` is editable. Provenance (``source``/``detected_by``/``dedup_key``)
    describes where the fact CAME FROM and must keep describing that even after a
    caseworker corrects the drafted change, so it stays immutable here; likewise
    ``confidence``, which is the producer's signal and not the reviewer's to
    rewrite. Validated by the same shape check as create.
    """

    intent = serializers.JSONField()

    def validate_intent(self, value):
        return validate_intent_shape(value)
