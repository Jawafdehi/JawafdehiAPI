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
