"""NGM judicial models (Django ORM).

Ported from the FastAPI/SQLAlchemy models (ngm.database.models). Table names are
pinned to the existing NGM schema so this maps onto the same `ngm` Postgres DB.
Only the read-plane core (courts, cases) is shown here; hearings/entities follow
the same pattern. The lakehouse (DuckDB/Iceberg) stays a separate service layer
queried outside the ORM — these models cover the relational court projection.
"""

from django.core.exceptions import ValidationError
from django.db import models

from jawafdehi_shared.entities.ids import build_courtcase_iri, is_valid_entity_iri


def validate_entity_iri(value: str) -> None:
    """Field validator: ``nes_id`` must be a canonical entity ``@id`` IRI.

    Clean-slate contract — the cross-service join key is the schema.org IRI
    ``https://jawafdehi.org/entity/<prefix>/<slug>`` (NES authority), not the old
    opaque ``entity:<prefix>/<slug>`` form. Null/blank is allowed (unresolved
    party); any non-empty value must validate. STRICT: the host must be the
    canonical ``iri_base()`` — a foreign host / scheme / port is rejected at this
    write boundary (data is clean-slate), so the stored key always matches NES.
    """
    if value in (None, ""):
        return
    if not is_valid_entity_iri(value):
        raise ValidationError(
            f"{value!r} is not a valid entity @id IRI "
            "(expected https://<base>/entity/<prefix>/<slug>)."
        )

# MANAGED-TABLE NOTE: every model here pins ``Meta.db_table`` to a table that
# ALREADY exists in the ``ngm`` Postgres schema (created/owned by the FastAPI
# /SQLAlchemy ingestion side). We deliberately keep ``Meta.managed`` at its
# default (True) — NOT ``managed = False`` — so that:
#   * the generated 0001_initial migration is a faithful, reviewable record of
#     the schema in Django terms, and
#   * the test database (APITestCase / pytest-django) can CREATE these tables.
# In the shared production ``ngm`` DB the SQLAlchemy side is the table authority,
# so the migration is applied with ``--fake`` there (Django records it as run
# without issuing DDL). The db_table pins guarantee both ORMs map to the same
# physical tables either way.


class Court(models.Model):
    identifier = models.CharField(max_length=50, primary_key=True)
    court_type = models.CharField(max_length=20, db_index=True)
    full_name_nepali = models.CharField(max_length=200)
    full_name_english = models.CharField(max_length=200, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "courts"

    def __str__(self) -> str:
        return self.identifier


class CourtCase(models.Model):
    # The source SQLAlchemy schema (ngm.database.models.CourtCase) declares a
    # COMPOSITE primary key on (case_number, court_identifier) and has NO `id`
    # column. Django would otherwise synthesize an `id = BigAutoField` PK and
    # emit `SELECT court_cases.id ...`, which fails against the real table
    # ("column court_cases.id does not exist"). Django 5.2+ supports composite
    # PKs, so declare the same composite key here and suppress the synthetic id.
    pk = models.CompositePrimaryKey("case_number", "court")
    case_number = models.CharField(max_length=50, db_index=True)
    court = models.ForeignKey(
        Court, on_delete=models.DO_NOTHING, db_column="court_identifier",
        related_name="cases",
    )
    registration_date_bs = models.CharField(max_length=20, null=True, blank=True)
    registration_date_ad = models.DateField(null=True, blank=True, db_index=True)
    case_type = models.CharField(max_length=200, null=True, blank=True, db_index=True)
    case_status = models.CharField(max_length=100, null=True, blank=True, db_index=True)
    plaintiff = models.TextField(null=True, blank=True)
    defendant = models.TextField(null=True, blank=True)
    # Canonical NES entity @id IRI (https://<base>/entity/<prefix>/<slug>) — the
    # cross-service join key. Widened to 300 for full IRIs; IRI-validated.
    nes_id = models.CharField(
        max_length=300, null=True, blank=True, db_index=True,
        validators=[validate_entity_iri],
    )
    extra_data = models.JSONField(null=True, blank=True)
    document_sources = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "court_cases"
        # The composite PK above (case_number + court_identifier) already
        # enforces uniqueness on the natural key, matching the FastAPI schema —
        # no separate unique_together (which would emit a redundant index that
        # the source table doesn't have).

    @property
    def iri(self) -> str:
        """The synthesized, canonical court-case ``@id`` IRI.

        ``https://<base>/courtcase/<court>/<case_number>`` — derived from the
        composite (court, case_number) natural key (no stored column). Distinct
        from the case record's *material* @id IRI
        (``/material/court/<court>.<case_number>``): this identifies the
        court-case row itself, the material IRI keys its JSON-LD CreativeWork.
        """
        return build_courtcase_iri(self.court_id, self.case_number)


class CourtCaseHearing(models.Model):
    """One causelist appearance for a case. Ported from the FastAPI
    ``court_case_hearings`` table. Keyed by an autoincrement ``id``; the
    (case_number, court) pair is the logical link to ``CourtCase`` (not a DB FK
    on the composite, matching the source schema's plain columns)."""

    id = models.AutoField(primary_key=True)
    case_number = models.CharField(max_length=50, db_index=True)
    court = models.ForeignKey(
        Court, on_delete=models.DO_NOTHING, db_column="court_identifier",
        related_name="hearings",
    )
    hearing_date_bs = models.CharField(max_length=20, db_index=True)
    hearing_date_ad = models.DateField(db_index=True)
    bench = models.CharField(max_length=100, null=True, blank=True)
    bench_type = models.CharField(max_length=100, null=True, blank=True)
    judge_names = models.TextField(null=True, blank=True)
    lawyer_names = models.TextField(null=True, blank=True)
    serial_no = models.CharField(max_length=20, null=True, blank=True)
    case_status = models.CharField(max_length=100, null=True, blank=True)
    decision_type = models.CharField(max_length=200, null=True, blank=True)
    remarks = models.TextField(null=True, blank=True)
    scraped_at = models.DateTimeField()
    extra_data = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "court_case_hearings"

    def __str__(self) -> str:
        return f"{self.case_number}@{self.hearing_date_bs}"


class CaseEntity(models.Model):
    """A single party (plaintiff/defendant) in a case, with optional NES
    resolution (``nes_id``). Ported from ``court_case_entities``."""

    id = models.AutoField(primary_key=True)
    case_number = models.CharField(max_length=50, db_index=True)
    court = models.ForeignKey(
        Court, on_delete=models.DO_NOTHING, db_column="court_identifier",
        related_name="case_entities",
    )
    side = models.CharField(max_length=20, db_index=True)  # plaintiff | defendant
    name = models.CharField(max_length=500)
    address = models.CharField(max_length=500, null=True, blank=True)
    # Canonical NES entity @id IRI (https://<base>/entity/<prefix>/<slug>) — the
    # cross-service join key. Widened to 300 for full IRIs; IRI-validated.
    nes_id = models.CharField(
        max_length=300, null=True, blank=True, db_index=True,
        validators=[validate_entity_iri],
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "court_case_entities"

    def __str__(self) -> str:
        return f"{self.side}:{self.name}"


class BlacklistedFirm(models.Model):
    """Firms blacklisted by PPMO (Public Procurement Monitoring Office). Ported
    from ``blacklisted_firms``."""

    id = models.AutoField(primary_key=True)
    firm_name = models.CharField(max_length=500, db_index=True)
    proprietor_name = models.CharField(max_length=500, null=True, blank=True)
    address = models.CharField(max_length=500, null=True, blank=True)
    blacklist_date_bs = models.CharField(max_length=20, null=True, blank=True)
    blacklist_date_ad = models.DateField(null=True, blank=True, db_index=True)
    effective_until_bs = models.CharField(max_length=20, null=True, blank=True)
    effective_until_ad = models.DateField(null=True, blank=True, db_index=True)
    duration = models.CharField(max_length=100, null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    recommending_office = models.CharField(max_length=500, null=True, blank=True)
    # Canonical NES entity @id IRI (https://<base>/entity/<prefix>/<slug>) — the
    # cross-service join key. Widened to 300 for full IRIs; IRI-validated.
    nes_id = models.CharField(
        max_length=300, null=True, blank=True, db_index=True,
        validators=[validate_entity_iri],
    )
    scraped_at = models.DateTimeField(auto_now_add=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "blacklisted_firms"

    def __str__(self) -> str:
        return self.firm_name
