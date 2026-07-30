from rest_framework import serializers

from .models import KNOWN_INTENT_TYPES, CaseUpdateProposal


def validate_intent_shape(intent):
    """Shallow shape check on create — enough to reject obviously malformed
    intents early. Deep validation (and the authority on what can be committed)
    happens at apply time in ``case_proposals.apply``."""
    if not isinstance(intent, dict):
        raise serializers.ValidationError("intent must be an object.")
    itype = intent.get("type")
    if itype not in KNOWN_INTENT_TYPES:
        raise serializers.ValidationError(
            f"Unknown intent type '{itype}'. Known: {list(KNOWN_INTENT_TYPES)}."
        )
    if itype == "append_timeline_entry":
        entry = intent.get("entry")
        if not isinstance(entry, dict) or not entry.get("date") or not entry.get("title"):
            raise serializers.ValidationError(
                "append_timeline_entry requires entry.date and entry.title."
            )
    elif itype == "set_status":
        if not intent.get("to"):
            raise serializers.ValidationError("set_status requires `to`.")
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
            "supersedes",
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
