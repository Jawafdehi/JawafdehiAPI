"""Service for creating draft cases from CIAA JSON data with deduplication."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from django.db import connection, transaction
from nepali.datetime import nepalidate

from jawafdehi_shared.entities.ids import is_valid_entity_iri

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipType,
)

logger = logging.getLogger(__name__)

_DOCUMENTSOURCE_REMOVED_MSG = (
    "This method creates/reads DocumentSource rows, which have been removed "
    "(ADR: cases own no documents). It must be rewired to create Material + "
    "CaseMaterialReference records before use. See "
    "docs/jawafdehi/sources-to-materials-prod-migration.md."
)


@dataclass
class ImportResult:
    status: str  # "created" | "skipped" | "failed"
    case_id: Optional[str] = None
    message: str = ""
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class CIAADraftCaseService:
    """Service for creating draft cases from CIAA JSON data with deduplication."""

    def __init__(self):
        """Initialize the service with empty caches."""
        self.stats = {
            # Number of defendant binds created from a valid NES id.
            "entities_bound": 0,
            # Defendants skipped because they had no valid NES id (NES is the
            # source of truth; binds cannot be created from a bare name).
            "entities_skipped_no_nes_id": 0,
            "sources_created": 0,
            "sources_reused": 0,
        }

    def import_case(self, ciaa_json: dict, dry_run: bool = False) -> ImportResult:
        """Import a single CIAA case from JSON. Returns ImportResult with status."""
        try:
            errors = self.validate_ciaa_json(ciaa_json)
            if errors:
                return ImportResult(
                    status="failed", message="JSON validation failed", errors=errors
                )

            case_data = self.map_json_to_case(ciaa_json)
            existing_case = self.check_case_exists(case_data.get("court_cases", []))

            if existing_case:
                return ImportResult(
                    status="skipped",
                    case_id=existing_case.slug,
                    message=f"Case already exists: {case_data['court_cases']}",
                )

            if dry_run:
                return ImportResult(
                    status="created",
                    message=f"Would create case: {case_data['title']} (dry-run)",
                )

            with transaction.atomic():
                case = Case.objects.create(
                    case_type=case_data["case_type"],
                    state=case_data["state"],
                    title=case_data["title"][:200],
                    case_start_date=case_data.get("case_start_date"),
                    case_end_date=case_data.get("case_end_date"),
                    court_cases=case_data.get("court_cases"),
                    notes=case_data.get("notes", ""),
                    missing_details=case_data.get("missing_details"),
                )
                logger.debug(f"Created case: {case.slug} - {case.title}")

                self.create_defendants(
                    ciaa_json.get("court_case", {}).get("defendants", []), case
                )
                self.create_material_evidence(ciaa_json, case)

            return ImportResult(
                status="created",
                case_id=case.slug,
                message=f"Created case: {case.title}",
            )

        except Exception as e:
            case_no = ciaa_json.get("case_no", "Unknown")
            error_msg = f"Import failed: {e!s}"
            logger.exception(f"Import failed for case {case_no}: {e!s}")
            return ImportResult(status="failed", message=error_msg, errors=[f"{e!s}"])

    def validate_ciaa_json(self, json_dict: dict) -> list[str]:
        """Validate CIAA JSON structure. Returns list of error messages."""
        errors = []
        if not json_dict.get("case_no"):
            errors.append("Missing required field: case_no")
        if not json_dict.get("case_title"):
            errors.append("Missing required field: case_title")
        if "court_case" not in json_dict:
            errors.append("Missing required field: court_case")

        meta = json_dict.get("meta", {})
        if "match_status" not in meta:
            errors.append("Missing required field: meta.match_status")
        elif meta["match_status"] not in ["confirmed", "needs_review", "unmatched"]:
            errors.append(f"Invalid match_status '{meta['match_status']}'")

        # Validate that CIAA cases have a special:* court case reference
        court_case = json_dict.get("court_case", {})
        court = court_case.get("court")
        case_no = court_case.get("case_no")
        if court and case_no:
            court_case_ref = f"{court}:{case_no}"
            if not court_case_ref.startswith("special:"):
                errors.append(
                    f"Missing required CIAA idempotency key: court_case must be a special:* reference, got '{court_case_ref}'"
                )

        return errors

    def _primary_ciaa_court_case(self, court_cases: list[str]) -> Optional[str]:
        """Extract the primary CIAA court case reference (special:*) for idempotency checks.

        CIAA cases are expected to have exactly one special:* entry in court_cases.
        This is the unique idempotency key and should be used for duplicate detection.

        Args:
            court_cases: List of court case references

        Returns:
            The special:* reference if found, otherwise None
        """
        for cc in court_cases:
            if cc.startswith("special:"):
                return cc
        return None

    def check_case_exists(self, court_cases: list[str]) -> Optional[Case]:
        """Check if case already exists by court_cases field. Returns existing Case or None."""
        primary = self._primary_ciaa_court_case(court_cases)
        if not primary:
            return None

        if connection.vendor == "postgresql":
            return Case.objects.filter(court_cases__contains=[primary]).first()
        else:
            for case in Case.objects.exclude(court_cases__isnull=True):
                if isinstance(case.court_cases, list) and primary in case.court_cases:
                    return case
        return None

    def map_json_to_case(self, ciaa_json: dict) -> dict:
        """Map CIAA JSON fields to Case model fields. Returns dict with case data."""
        case_data = {}
        case_title = ciaa_json.get("case_title", "")
        case_no = ciaa_json.get("case_no", "")

        title_base = case_title[:180] if len(case_title) > 180 else case_title
        case_data["title"] = (
            f"{title_base} ({case_no})"[:200] if case_no else title_base[:200]
        )
        case_data["case_type"] = CaseType.CORRUPTION
        case_data["state"] = CaseState.DRAFT

        court_case = ciaa_json.get("court_case", {})

        # Parse dates
        if reg_date := court_case.get("registration_date_ad"):
            try:
                case_data["case_start_date"] = datetime.strptime(
                    reg_date, "%Y-%m-%d"
                ).date()
            except (ValueError, TypeError):
                case_data["case_start_date"] = None
        else:
            case_data["case_start_date"] = None

        if faisala_date := court_case.get("faisala_date_ad"):
            try:
                case_data["case_end_date"] = datetime.strptime(
                    faisala_date, "%Y-%m-%d"
                ).date()
            except (ValueError, TypeError):
                case_data["case_end_date"] = self.convert_bs_to_ad(
                    court_case.get("faisala_date_bs")
                )
        else:
            case_data["case_end_date"] = self.convert_bs_to_ad(
                court_case.get("faisala_date_bs")
            )

        # Build court_cases list
        court_cases = []
        if (
            court_case
            and (court := court_case.get("court"))
            and (cn := court_case.get("case_no"))
        ):
            court_cases.append(f"{court}:{cn}")

        if appealed := ciaa_json.get("appealed_case"):
            if (ac := appealed.get("court")) and (acn := appealed.get("case_no")):
                court_cases.append(f"{ac}:{acn}")

        case_data["court_cases"] = court_cases
        case_data["missing_details"] = (
            "This case has match_status='needs_review' and requires verification."
            if ciaa_json.get("meta", {}).get("match_status") == "needs_review"
            else None
        )

        return case_data

    def convert_bs_to_ad(self, bs_date_str: str) -> Optional[datetime]:
        """Convert Bikram Sambat date string to AD date. Returns date or None."""
        if not bs_date_str:
            return None
        try:
            devanagari_to_ascii = str.maketrans("०१२३४५६७८९", "0123456789")
            normalized = bs_date_str.translate(devanagari_to_ascii).replace("/", "-")
            parts = normalized.split("-")
            if len(parts) != 3:
                return None
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            return nepalidate(year, month, day).to_datetime().date()
        except Exception:
            return None

    def create_defendants(self, defendants: list[dict], case: Case) -> list[str]:
        """Bind defendants that carry a valid NES id to the case as ACCUSED.

        NES is the single source of truth for entities, and a Case<->entity
        bind requires a valid canonical NES @id IRI
        (``https://jawafdehi.org/entity/<prefix>/<slug>``) — there is no name
        fallback. Defendants without a resolved ``nes_id`` are
        skipped (and counted), because we will not invent local entity data from
        a bare name. Resolving plaintext defendant names to NES ids (including
        the privacy carve-out registration of private parties) happens upstream.

        Returns the list of bound NES ids.
        """
        bound: list[str] = []
        for defendant in defendants:
            nes_id = (defendant.get("nes_id") or "").strip()
            if not is_valid_entity_iri(nes_id):
                self.stats["entities_skipped_no_nes_id"] += 1
                logger.debug(
                    "Skipping defendant without a valid NES id: %r",
                    defendant.get("name", ""),
                )
                continue
            CaseEntityRelationship.objects.get_or_create(
                case=case,
                nes_id=nes_id,
                relationship_type=RelationshipType.ACCUSED,
                defaults={"notes": ""},
            )
            self.stats["entities_bound"] += 1
            bound.append(nes_id)
        return bound

    def create_material_evidence(self, ciaa_json: dict, case: Case) -> list[str]:
        """Ingest CIAA press releases, charge sheets, and court orders as evidence.

        Each becomes an NGM Material (single-source upsert, gate bypassed) bound to
        the case via a ``CaseMaterialReference`` (ADR: cases own no documents).
        Returns the list of material IRIs bound. Replaces the old
        ``create_document_sources`` (which created DocumentSource rows + a
        ``Case.evidence`` JSON list).
        """
        from cases.services.material_ingest import ingest_source_as_evidence

        iris: list[str] = []

        def _bind(*, title, url, source_type, additional_details, publication_date=None):
            iri = ingest_source_as_evidence(
                case,
                title=(title or "")[:300],
                url=url,
                source_type=source_type,
                additional_details=additional_details,
                publication_date=publication_date,
            )
            if iri and iri not in iris:
                iris.append(iri)

        # Press releases
        for pr in ciaa_json.get("ciaa", {}).get("press_releases", []):
            _bind(
                title=pr.get("title", "CIAA Press Release"),
                url=pr.get("url", ""),
                source_type="CIAA_PRESS_RELEASE",
                additional_details=(
                    f"CIAA Press Release (ID: {pr.get('release_id', 'N/A')})"
                ),
                publication_date=self.convert_bs_to_ad(pr.get("date", "")),
            )

        # AG charge sheets (abhiyogPatras)
        for ap in ciaa_json.get("ciaa", {}).get("abhiyogPatras", []):
            pdf_url = (ap.get("pdf_url") or "").strip()
            encoded_url = self._encode_url(pdf_url) if pdf_url else ""
            _bind(
                title=ap.get("title", "AG Charge Sheet"),
                url=[encoded_url] if encoded_url else [],
                source_type="AG_ABHIYOG_PATRA",
                additional_details=f"AG Charge Sheet - {ap.get('case_number', 'N/A')}",
                publication_date=self.convert_bs_to_ad(ap.get("filing_date", "")),
            )

        # Court orders (faisala links)
        for idx, faisala_url in enumerate(
            ciaa_json.get("court_case", {}).get("faisala_link", []), 1
        ):
            if faisala_url:
                _bind(
                    title=f"Court Order - {ciaa_json.get('case_no', 'Unknown')}",
                    url=faisala_url,
                    source_type="COURT_ORDER",
                    additional_details=f"Court Order/Verdict (Document {idx})",
                )

        return iris

    @staticmethod
    def _encode_url(pdf_url: str) -> str:
        """Percent-encode a raw PDF URL's path/query (mirrors the old ingester)."""
        import urllib.parse

        parsed = urllib.parse.urlsplit(pdf_url.strip())
        return urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                urllib.parse.quote(parsed.path, safe="/"),
                urllib.parse.quote(parsed.query, safe="=&"),
                urllib.parse.quote(parsed.fragment, safe=""),
            )
        )
