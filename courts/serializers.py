"""DRF serializers for the NGM read plane."""
from rest_framework import serializers

from materials.jsonld import court_case_material_iri

from . import case_status as cs
from .models import (
    BlacklistedFirm,
    CaseEntity,
    Court,
    CourtCase,
    CourtCaseHearing,
    validate_entity_iri,
)


class CourtSerializer(serializers.ModelSerializer):
    class Meta:
        model = Court
        fields = ["identifier", "court_type", "full_name_nepali", "full_name_english"]


class CourtCaseSerializer(serializers.ModelSerializer):
    court_identifier = serializers.CharField(source="court_id")
    # The case record's schema.org material @id IRI — resolvable JSON-LD at
    # GET /api/materials/?iri=<material_id> (and /api/materials/court/<ident>).
    material_id = serializers.SerializerMethodField()
    # The court-case row's synthesized @id IRI (/courtcase/<court>/<case_number>),
    # distinct from the material IRI above. Derived from the composite key.
    courtcase_iri = serializers.CharField(source="iri", read_only=True)
    # Whitelisted against cs.VERDICT_TYPES — see get_verdict_type below.
    verdict_type = serializers.SerializerMethodField()

    class Meta:
        model = CourtCase
        fields = [
            "case_number", "court_identifier", "registration_date_bs",
            "registration_date_ad", "case_type", "case_status",
            "plaintiff", "defendant", "nes_id", "document_sources",
            "material_id", "courtcase_iri",
            "verdict_type", "verdict_date_bs", "verdict_date_ad",
        ]

    def get_material_id(self, obj: CourtCase) -> str:
        return court_case_material_iri(obj.court_id, obj.case_number)

    def get_verdict_type(self, obj: CourtCase) -> str | None:
        """Expose ``verdict_type`` only when it is a real enum member.

        The column carries no DB constraint and historic Supreme enrichment
        wrote raw portal text into it (see ``cs.VERDICT_TYPES``). Publishing
        that verbatim would state an outcome the court never reached — a bench
        referral such as ``पूर्ण इजलासमा पेस हुने`` ("to be presented to the full
        bench") reads like a disposition but means the case is still live.

        Unrecognised values become ``None`` rather than leaking: the honest
        public claim is "we hold no classified verdict for this docket", which
        is what a null says. The raw value stays in the database for the DQ
        backfill to repair.
        """
        value = (obj.verdict_type or "").strip()
        return value if value in cs.VERDICT_TYPES else None


class CourtCaseHearingSerializer(serializers.ModelSerializer):
    court_identifier = serializers.CharField(source="court_id")

    class Meta:
        model = CourtCaseHearing
        fields = [
            "id", "case_number", "court_identifier",
            "hearing_date_bs", "hearing_date_ad",
            "bench", "bench_type", "judge_names", "lawyer_names",
            "serial_no", "case_status", "decision_type", "remarks",
            "scraped_at", "extra_data",
        ]


class CaseEntitySerializer(serializers.ModelSerializer):
    court_identifier = serializers.CharField(source="court_id")

    class Meta:
        model = CaseEntity
        fields = [
            "id", "case_number", "court_identifier",
            "side", "name", "address", "nes_id",
        ]


class CourtCaseWriteSerializer(serializers.ModelSerializer):
    """Write serializer for create (POST /cases/) and composite-key update.

    The model has a COMPOSITE primary key (case_number, court) and the FK
    ``court`` is stored in the ``court_identifier`` column — so a dedicated write
    serializer keeps the FK/PK handling clean (the read serializer exposes derived
    SerializerMethodFields like ``material_id`` that don't round-trip on write).

    ``court_identifier`` is the wire field; it maps to the ``court`` FK via a
    PrimaryKeyRelatedField (queryset=Court). ``nes_id`` is IRI-validated by the
    model field's ``validators=[validate_entity_iri]`` (surfaced via
    ``full_clean()`` in the view), but DRF's run_validators also applies the
    field validator on serializer validation, so a non-IRI value is a 400 here.
    """

    court_identifier = serializers.PrimaryKeyRelatedField(
        queryset=Court.objects.all(), source="court"
    )

    class Meta:
        model = CourtCase
        fields = [
            "case_number", "court_identifier",
            "registration_date_bs", "registration_date_ad",
            "case_type", "case_status", "plaintiff", "defendant",
            "nes_id", "extra_data", "document_sources",
        ]

    def validate_nes_id(self, value):
        # Mirror the model field validator at the serializer boundary so an
        # invalid (non-IRI, non-blank) nes_id is a 400, not a 500 on save.
        validate_entity_iri(value)
        return value


class BlacklistedFirmSerializer(serializers.ModelSerializer):
    class Meta:
        model = BlacklistedFirm
        fields = [
            "id", "firm_name", "proprietor_name", "address",
            "blacklist_date_bs", "blacklist_date_ad",
            "effective_until_bs", "effective_until_ad",
            "duration", "reason", "recommending_office", "nes_id",
        ]


class BlacklistedFirmWriteSerializer(serializers.ModelSerializer):
    """Write serializer for the ``POST /ingestion/firms`` upsert.

    ``firm_name`` + ``blacklist_date_bs`` are the natural key (both required);
    the rest are optional detail. ``id`` is excluded so it never round-trips.

    ``blacklist_date_bs`` is redeclared explicitly because the model field is
    ``blank=True`` and the model's ``UniqueConstraint`` on the pair makes DRF
    otherwise (a) clash ``required`` with a derived ``default`` and (b) auto-add
    a ``UniqueTogetherValidator`` that would 400 the idempotent re-POST. The VIEW
    owns idempotency (existing-check + IntegrityError), so serializer-level
    uniqueness is disabled (``validators = []``)."""

    blacklist_date_bs = serializers.CharField(max_length=20)

    class Meta:
        model = BlacklistedFirm
        fields = [
            "firm_name", "proprietor_name", "address",
            "blacklist_date_bs", "blacklist_date_ad",
            "effective_until_bs", "effective_until_ad",
            "duration", "reason", "recommending_office", "nes_id",
        ]
        validators = []
