"""Silver-zone table definitions for the NGM lakehouse (documented constants).

The silver zone is "one table family per source-type" (see
``ngm-data-lake-plan.md`` section 2.2), NOT one schema for everything. Each
table carries:
- typed columns for what we *query and filter* on,
- an ``extra_data`` JSON/variant column for everything else (keeps the lake
  flexible — "ingest first, model later"),
- a ``provenance`` struct (source URL, fetch method, TLS status, OCR engine /
  confidence — per the shared acquisition pipeline),
- ``nes_id`` / entity-ref columns linking to the Nepal Entity Service,
- bronze-lineage columns (``bronze_uri``, ``ingested_at``) and a natural-key
  marker used for idempotent upsert.

These are declared as data, not as live Iceberg DDL: a list of
:class:`ColumnSpec` per table. ``engine.py`` / ``medallion.py`` translate them
into ``CREATE TABLE ... (... ) PARTITIONED BY (...)`` against the REST catalog
when a live catalog is available. Keeping them as plain constants makes the
contract testable (``tests/lakehouse/test_schema.py``) without a catalog.

Types use the Iceberg/DuckDB primitive vocabulary
(``STRING``/``LONG``/``INT``/``DATE``/``TIMESTAMP``/``BOOLEAN``/``DOUBLE``/
``JSON``) — the type names are documentation-grade; the catalog mapping happens
at DDL-generation time.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Iceberg/DuckDB primitive type names we use in the silver schemas. JSON stands
# in for the flexible variant/struct columns (extra_data, provenance, links).
ICEBERG_TYPES = frozenset(
    {
        "STRING",
        "INT",
        "LONG",
        "DOUBLE",
        "DECIMAL",
        "BOOLEAN",
        "DATE",
        "TIMESTAMP",
        "JSON",
    }
)


@dataclass(frozen=True)
class ColumnSpec:
    """One silver column: name + type, optional doc + partition/key flags."""

    name: str
    type: str
    doc: str = ""
    # Part of the natural key used for idempotent silver upsert (re-scrape safe).
    natural_key: bool = False
    # Whether this column participates in the table's partition spec.
    partition: bool = False


@dataclass(frozen=True)
class TableSpec:
    """A silver table: ordered columns + the partition transform list.

    ``partition_by`` entries are Iceberg partition transforms over real columns,
    e.g. ``"court_identifier"`` (identity) or ``"day(registration_date_ad)"``.
    """

    name: str
    doc: str
    columns: list[ColumnSpec]
    partition_by: list[str] = field(default_factory=list)

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    @property
    def natural_key(self) -> list[str]:
        return [c.name for c in self.columns if c.natural_key]


# --- shared column blocks ---------------------------------------------------
# Every silver table ends with these. ``provenance`` and ``extra_data`` are the
# flexibility valves; bronze-lineage links the row back to its raw capture.

_PROVENANCE_COLS = [
    ColumnSpec(
        "provenance",
        "JSON",
        "Struct: source_url, fetch_method, tls_status, ocr_engine, "
        "ocr_confidence, scraped_at — from the shared acquisition pipeline.",
    ),
    ColumnSpec(
        "bronze_uri",
        "STRING",
        "R2 URI of the raw bronze capture this row derives from.",
    ),
    ColumnSpec(
        "extra_data",
        "JSON",
        "Everything not promoted to a typed column (lake flexibility).",
    ),
    ColumnSpec(
        "ingested_at", "TIMESTAMP", "When this silver row was written/upserted."
    ),
]


def _with_common(cols: list[ColumnSpec]) -> list[ColumnSpec]:
    """Append the shared provenance/lineage/extra_data tail to a column list."""
    return [*cols, *_PROVENANCE_COLS]


# --- court family (the existing relational schema, demoted to a projection) --

COURT_CASES = TableSpec(
    name="court_cases",
    doc=(
        "Court cases — silver projection of the current ``court_cases`` table. "
        "Natural key (court_identifier, case_number). The relational Postgres "
        "view is derived from this."
    ),
    columns=_with_common(
        [
            ColumnSpec(
                "court_identifier",
                "STRING",
                "FK to court_courts.identifier.",
                natural_key=True,
                partition=True,
            ),
            ColumnSpec(
                "case_number", "STRING", "e.g. '082-OA-0503'.", natural_key=True
            ),
            ColumnSpec("registration_date_bs", "STRING", "BS date 'YYYY-MM-DD'."),
            ColumnSpec(
                "registration_date_ad",
                "DATE",
                "AD date for range queries.",
                partition=True,
            ),
            ColumnSpec("case_type", "STRING", "मुद्दाको किसिम."),
            ColumnSpec("division", "STRING", "फाँट."),
            ColumnSpec("category", "STRING", "bench/category."),
            ColumnSpec("section", "STRING", "धारा / section."),
            ColumnSpec(
                "plaintiff", "STRING", "May hold multiple parties (समेत-joined)."
            ),
            ColumnSpec(
                "defendant", "STRING", "May hold multiple parties (समेत-joined)."
            ),
            ColumnSpec(
                "original_case_number", "STRING", "For appeals / related cases."
            ),
            ColumnSpec("case_id", "STRING", "Internal court id (district courts)."),
            ColumnSpec("priority", "STRING", "e.g. 'सरल' (fast-track)."),
            ColumnSpec("registration_number", "STRING", "Enriched."),
            ColumnSpec("case_status", "STRING", "e.g. 'चालु', 'फैसला भएको'."),
            ColumnSpec("verdict_date_bs", "STRING"),
            ColumnSpec("verdict_date_ad", "DATE"),
            ColumnSpec("verdict_judge", "STRING"),
            ColumnSpec("verdict_type", "STRING", "e.g. 'फैसला', 'आदेश'."),
            ColumnSpec("case_subject", "STRING", "मुद्दाको बिषय."),
            ColumnSpec("hearing_count", "STRING", "पेशी चढेको संख्या."),
            ColumnSpec(
                "status", "STRING", "Enrichment status: pending/enriched/failed."
            ),
            ColumnSpec("enriched_at", "TIMESTAMP"),
            ColumnSpec(
                "document_sources",
                "JSON",
                "Roled-link DocumentSource list (court orders).",
            ),
        ]
    ),
    partition_by=["court_identifier", "day(registration_date_ad)"],
)

COURT_HEARINGS = TableSpec(
    name="court_hearings",
    doc="Hearing records (one row per causelist appearance). Projection of court_case_hearings.",
    columns=_with_common(
        [
            ColumnSpec("court_identifier", "STRING", natural_key=True, partition=True),
            ColumnSpec("case_number", "STRING", natural_key=True),
            ColumnSpec("hearing_date_bs", "STRING", natural_key=True),
            ColumnSpec("hearing_date_ad", "DATE", partition=True),
            ColumnSpec(
                "serial_no",
                "STRING",
                "Order in causelist (क, ख, 1, 2…).",
                natural_key=True,
            ),
            ColumnSpec("bench", "STRING"),
            ColumnSpec("bench_type", "STRING"),
            ColumnSpec("judge_names", "STRING"),
            ColumnSpec("lawyer_names", "STRING"),
            ColumnSpec("case_status", "STRING"),
            ColumnSpec("decision_type", "STRING"),
            ColumnSpec("remarks", "STRING"),
            ColumnSpec("scraped_at", "TIMESTAMP"),
        ]
    ),
    partition_by=["court_identifier", "day(hearing_date_ad)"],
)

COURT_ENTITIES = TableSpec(
    name="court_entities",
    doc="Case parties (plaintiff/defendant) + NES resolution. Projection of court_case_entities.",
    columns=_with_common(
        [
            ColumnSpec("court_identifier", "STRING", natural_key=True, partition=True),
            ColumnSpec("case_number", "STRING", natural_key=True),
            ColumnSpec(
                "side", "STRING", "'plaintiff' | 'defendant'.", natural_key=True
            ),
            ColumnSpec("name", "STRING", "Party name.", natural_key=True),
            ColumnSpec("address", "STRING"),
            ColumnSpec(
                "nes_id", "STRING", "Nepal Entity Service id (entity resolution)."
            ),
        ]
    ),
    partition_by=["court_identifier"],
)

COURTS = TableSpec(
    name="courts",
    doc="Court master (small bounded set ~97 rows). Unpartitioned.",
    columns=_with_common(
        [
            ColumnSpec(
                "identifier",
                "STRING",
                "e.g. 'kathmandudc', 'supreme'.",
                natural_key=True,
            ),
            ColumnSpec("court_type", "STRING", "district/high/supreme/special."),
            ColumnSpec("full_name_nepali", "STRING"),
            ColumnSpec("full_name_english", "STRING"),
        ]
    ),
)

CHARGE_SHEETS = TableSpec(
    name="charge_sheets",
    doc=(
        "CIAA charge sheets / press-release prosecutions (अभियोगपत्र). A new "
        "governance source-type that previously had no relational home."
    ),
    columns=_with_common(
        [
            ColumnSpec(
                "charge_sheet_id",
                "STRING",
                "Stable id, e.g. 'ngm:ciaa-charge:1234'.",
                natural_key=True,
            ),
            ColumnSpec("title", "STRING"),
            ColumnSpec("filed_date_bs", "STRING"),
            ColumnSpec("filed_date_ad", "DATE", partition=True),
            ColumnSpec(
                "court_identifier", "STRING", "Special Court usually.", partition=True
            ),
            ColumnSpec("case_number", "STRING", "Linked court case, when known."),
            ColumnSpec(
                "accused_names", "JSON", "List of accused (name + nes_id) structs."
            ),
            ColumnSpec("offence", "STRING", "e.g. 'भ्रष्टाचार ( रिसवत(घुस) )'."),
            ColumnSpec(
                "amount_npr", "DOUBLE", "Alleged corruption amount in NPR, when stated."
            ),
            ColumnSpec("summary", "STRING"),
        ]
    ),
    partition_by=["filed_date_ad"],
)

# --- procurement / contracts family -----------------------------------------

PROCUREMENT_TENDERS = TableSpec(
    name="procurement_tenders",
    doc="PPMO/e-GP tender notices (बोलपत्र सूचना).",
    columns=_with_common(
        [
            ColumnSpec(
                "tender_id", "STRING", "e-GP / PPMO tender id.", natural_key=True
            ),
            ColumnSpec("public_entity", "STRING", "Procuring agency."),
            ColumnSpec("public_entity_nes_id", "STRING"),
            ColumnSpec("title", "STRING"),
            ColumnSpec("category", "STRING", "goods/works/services/consultancy."),
            ColumnSpec("notice_date_bs", "STRING"),
            ColumnSpec("notice_date_ad", "DATE", partition=True),
            ColumnSpec("submission_deadline_ad", "DATE"),
            ColumnSpec("estimated_cost_npr", "DOUBLE"),
            ColumnSpec("fiscal_year", "STRING", "e.g. '2081/82'.", partition=True),
            ColumnSpec("status", "STRING"),
        ]
    ),
    partition_by=["fiscal_year"],
)

PROCUREMENT_AWARDS = TableSpec(
    name="procurement_awards",
    doc="Contract awards resulting from tenders (ठेक्का स्वीकृति).",
    columns=_with_common(
        [
            ColumnSpec("award_id", "STRING", natural_key=True),
            ColumnSpec("tender_id", "STRING", "FK to procurement_tenders.tender_id."),
            ColumnSpec("public_entity", "STRING"),
            ColumnSpec("contractor_name", "STRING"),
            ColumnSpec("contractor_nes_id", "STRING"),
            ColumnSpec("award_amount_npr", "DOUBLE"),
            ColumnSpec("award_date_bs", "STRING"),
            ColumnSpec("award_date_ad", "DATE", partition=True),
            ColumnSpec("fiscal_year", "STRING", partition=True),
            ColumnSpec("contract_status", "STRING"),
        ]
    ),
    partition_by=["fiscal_year"],
)

PROCUREMENT_CONTRACTORS = TableSpec(
    name="procurement_contractors",
    doc=(
        "Contractor/firm registry incl. PPMO blacklist (कालोसूची). Subsumes the "
        "current ``blacklisted_firms`` table."
    ),
    columns=_with_common(
        [
            ColumnSpec("firm_name", "STRING", natural_key=True),
            ColumnSpec("proprietor_name", "STRING"),
            ColumnSpec("address", "STRING"),
            ColumnSpec("nes_id", "STRING"),
            ColumnSpec("is_blacklisted", "BOOLEAN"),
            ColumnSpec("blacklist_date_bs", "STRING", natural_key=True),
            ColumnSpec("blacklist_date_ad", "DATE", partition=True),
            ColumnSpec("effective_until_bs", "STRING"),
            ColumnSpec("effective_until_ad", "DATE"),
            ColumnSpec("duration", "STRING"),
            ColumnSpec("reason", "STRING"),
            ColumnSpec("recommending_office", "STRING"),
        ]
    ),
    partition_by=["blacklist_date_ad"],
)

# --- budget / projects family (federal/provincial/local) ---------------------

PROJECTS = TableSpec(
    name="projects",
    doc="Government projects/programmes (आयोजना) across the three tiers.",
    columns=_with_common(
        [
            ColumnSpec("project_id", "STRING", natural_key=True),
            ColumnSpec("name", "STRING"),
            ColumnSpec("ministry", "STRING"),
            ColumnSpec("implementing_agency", "STRING"),
            ColumnSpec("level", "STRING", "federal/provincial/local.", partition=True),
            ColumnSpec("province", "STRING"),
            ColumnSpec("district", "STRING"),
            ColumnSpec("fiscal_year", "STRING", partition=True),
            ColumnSpec("allocated_npr", "DOUBLE"),
            ColumnSpec("expenditure_npr", "DOUBLE"),
            ColumnSpec("status", "STRING"),
        ]
    ),
    partition_by=["fiscal_year", "level"],
)

BUDGET_LINES = TableSpec(
    name="budget_lines",
    doc="Red-book budget line items (बजेट शीर्षक) by fiscal year and tier.",
    columns=_with_common(
        [
            ColumnSpec("budget_line_id", "STRING", natural_key=True),
            ColumnSpec("fiscal_year", "STRING", natural_key=True, partition=True),
            ColumnSpec("level", "STRING", "federal/provincial/local.", partition=True),
            ColumnSpec("ministry", "STRING"),
            ColumnSpec("program", "STRING"),
            ColumnSpec(
                "project_id", "STRING", "FK to projects.project_id, when applicable."
            ),
            ColumnSpec("budget_head", "STRING", "शीर्षक नम्बर."),
            ColumnSpec("source", "STRING", "GoN/foreign-grant/foreign-loan."),
            ColumnSpec("allocated_npr", "DOUBLE"),
            ColumnSpec("revised_npr", "DOUBLE"),
            ColumnSpec("actual_npr", "DOUBLE"),
        ]
    ),
    partition_by=["fiscal_year", "level"],
)

# --- accountability family (audits, asset declarations) ----------------------

AUDIT_FINDINGS = TableSpec(
    name="audit_findings",
    doc="OAG (महालेखा) audit findings / बेरुजु (irregularities).",
    columns=_with_common(
        [
            ColumnSpec("finding_id", "STRING", natural_key=True),
            ColumnSpec("audited_entity", "STRING"),
            ColumnSpec("audited_entity_nes_id", "STRING"),
            ColumnSpec("fiscal_year", "STRING", partition=True),
            ColumnSpec(
                "finding_type",
                "STRING",
                "beruju category (advance/irregular/recoverable…).",
            ),
            ColumnSpec("amount_npr", "DOUBLE"),
            ColumnSpec("description", "STRING"),
            ColumnSpec("report_date_ad", "DATE", partition=True),
            ColumnSpec("status", "STRING", "settled/outstanding."),
        ]
    ),
    partition_by=["fiscal_year"],
)

ASSET_DECLARATIONS = TableSpec(
    name="asset_declarations",
    doc="Public-official property declarations (सम्पत्ति विवरण).",
    columns=_with_common(
        [
            ColumnSpec("declaration_id", "STRING", natural_key=True),
            ColumnSpec("official_name", "STRING"),
            ColumnSpec("official_nes_id", "STRING"),
            ColumnSpec("position", "STRING"),
            ColumnSpec("office", "STRING"),
            ColumnSpec("fiscal_year", "STRING", partition=True),
            ColumnSpec("declaration_date_ad", "DATE"),
            ColumnSpec("declared_assets", "JSON", "Structured asset line items."),
        ]
    ),
    partition_by=["fiscal_year"],
)

# --- registries & decisions family -------------------------------------------

COMPANY_REGISTRY = TableSpec(
    name="company_registry",
    doc="OCR (कम्पनी रजिष्ट्रार) registered companies/financial registry.",
    columns=_with_common(
        [
            ColumnSpec("registration_no", "STRING", natural_key=True),
            ColumnSpec("company_name", "STRING"),
            ColumnSpec("nes_id", "STRING"),
            ColumnSpec("company_type", "STRING", "private/public/non-profit."),
            ColumnSpec("registration_date_ad", "DATE", partition=True),
            ColumnSpec("status", "STRING", "active/struck-off/liquidated."),
            ColumnSpec("registered_office", "STRING"),
            ColumnSpec("paid_up_capital_npr", "DOUBLE"),
            ColumnSpec(
                "directors", "JSON", "List of director (name + nes_id) structs."
            ),
        ]
    ),
    partition_by=["status"],
)

MINISTERIAL_DECISIONS = TableSpec(
    name="ministerial_decisions",
    doc="Council-of-Ministers / ministry decisions (मन्त्रिपरिषद् निर्णय).",
    columns=_with_common(
        [
            ColumnSpec("decision_id", "STRING", natural_key=True),
            ColumnSpec("ministry", "STRING"),
            ColumnSpec("decision_date_bs", "STRING"),
            ColumnSpec("decision_date_ad", "DATE", partition=True),
            ColumnSpec("subject", "STRING"),
            ColumnSpec("summary", "STRING"),
            ColumnSpec(
                "related_entities", "JSON", "Named entities (name + nes_id) structs."
            ),
        ]
    ),
    partition_by=["day(decision_date_ad)"],
)

GAZETTE_ENTRIES = TableSpec(
    name="gazette_entries",
    doc="Nepal Gazette (राजपत्र) notices/appointments.",
    columns=_with_common(
        [
            ColumnSpec("gazette_id", "STRING", natural_key=True),
            ColumnSpec("part", "STRING", "खण्ड."),
            ColumnSpec("publication_date_bs", "STRING"),
            ColumnSpec("publication_date_ad", "DATE", partition=True),
            ColumnSpec("title", "STRING"),
            ColumnSpec("body", "STRING"),
        ]
    ),
    partition_by=["day(publication_date_ad)"],
)


# --- registry ----------------------------------------------------------------
# All silver tables, keyed by table name. ``medallion.refresh_gold`` and
# ``engine`` DDL generation iterate this; tests validate every entry.

SILVER_TABLES: dict[str, TableSpec] = {
    t.name: t
    for t in (
        COURTS,
        COURT_CASES,
        COURT_HEARINGS,
        COURT_ENTITIES,
        CHARGE_SHEETS,
        PROCUREMENT_TENDERS,
        PROCUREMENT_AWARDS,
        PROCUREMENT_CONTRACTORS,
        PROJECTS,
        BUDGET_LINES,
        AUDIT_FINDINGS,
        ASSET_DECLARATIONS,
        COMPANY_REGISTRY,
        MINISTERIAL_DECISIONS,
        GAZETTE_ENTRIES,
    )
}


def get_table(name: str) -> TableSpec:
    """Look up a silver table spec by name, raising ``KeyError`` if unknown."""
    return SILVER_TABLES[name]
