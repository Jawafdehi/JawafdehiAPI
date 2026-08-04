"""Service for creating draft cases from CIAA JSON data with deduplication."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from django.core.exceptions import ValidationError
from django.db import transaction

from jawafdehi_shared.dates import bs_to_ad
from jawafdehi_shared.entities.ids import is_valid_entity_iri

from cases.models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    CaseType,
    RelationshipType,
)
from cases.validators import courtcase_iri_from_parts, parse_courtcase_ref
from courts.normalize import parse_stated_defendant_count

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
            # Cases whose NGM defendant roster was detected as truncated at
            # import (accused list incomplete vs the court's stated total).
            "cases_flagged_truncated": 0,
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

                court_case = ciaa_json.get("court_case") or {}
                defendants = court_case.get("defendants", [])
                bound = self.create_defendants(defendants, case)
                self._flag_truncated_roster(
                    court_case, case, parsed_count=len(defendants), bound_count=len(bound)
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

        # Validate that CIAA cases reference the Special Court (the dedup
        # anchor). Normalized the same way courtcase_iri_from_parts builds the
        # IRI (strip + lower), so validation and construction can't disagree.
        # An absent court/case_no stays allowed — press-release-stage cases
        # legitimately import without a court reference yet.
        court_case = json_dict.get("court_case") or {}
        court = (court_case.get("court") or "").strip().lower()
        case_no = (court_case.get("case_no") or "").strip()
        if court and case_no and court != "special":
            errors.append(
                "Missing required CIAA idempotency key: court_case must be a "
                f"Special Court reference, got court '{court_case.get('court')}'"
            )

        return errors

    def _primary_ciaa_court_case(self, court_cases: list[str]) -> Optional[str]:
        """Extract the primary CIAA court case reference (Special Court) for idempotency checks.

        CIAA cases are expected to have exactly one Special Court entry in
        court_cases (@id IRIs). This is the unique idempotency
        key and should be used for duplicate detection.

        Args:
            court_cases: List of court case references

        Returns:
            The Special Court reference if found, otherwise None
        """
        for cc in court_cases:
            parsed = parse_courtcase_ref(cc)
            if parsed and parsed[0] == "special":
                return cc
        return None

    def check_case_exists(self, court_cases: list[str]) -> Optional[Case]:
        """Check if a case already references the same primary court case.

        Matches on the CaseCourtCaseReference join by the canonical @id IRI
        (``court_cases`` entries are IRIs by the time they reach here — built
        in ``map_json_to_case``). Returns the existing Case or None.
        """
        primary = self._primary_ciaa_court_case(court_cases)
        if not primary:
            return None
        return Case.objects.filter(courtcase_references__courtcase_iri=primary).first()

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

        # Build court_cases list — canonical @id IRIs (the only stored form),
        # from the CIAA JSON's (court, case_no) pairs. A malformed PRIMARY ref
        # raises ValidationError -> the importer records the case as failed
        # (the Special Court number is the dedup anchor). The APPEALED ref is
        # best-effort: scraped appeal fields are noisier (Devanagari digits,
        # free-text), and losing the whole import — primary court data,
        # defendants, evidence — over a secondary ref is the wrong trade.
        court_cases = []
        if (
            court_case
            and (court := court_case.get("court"))
            and (cn := court_case.get("case_no"))
        ):
            court_cases.append(courtcase_iri_from_parts(court, cn))

        if appealed := ciaa_json.get("appealed_case"):
            if (ac := appealed.get("court")) and (acn := appealed.get("case_no")):
                try:
                    court_cases.append(courtcase_iri_from_parts(ac, acn))
                except ValidationError:
                    logger.warning(
                        "Skipping uncanonicalizable appealed-case ref %r:%r",
                        ac,
                        acn,
                    )

        case_data["court_cases"] = court_cases
        case_data["missing_details"] = (
            "This case has match_status='needs_review' and requires verification."
            if ciaa_json.get("meta", {}).get("match_status") == "needs_review"
            else None
        )

        return case_data

    def convert_bs_to_ad(self, bs_date_str: str) -> Optional[datetime]:
        """Convert Bikram Sambat date string to AD date. Returns date or None.

        Delegates to the shared :func:`jawafdehi_shared.dates.bs_to_ad` contract
        (which folds in this method's Devanagari-digit/``/``-separator tolerance);
        retained as a thin instance method so existing callers are unchanged.
        """
        return bs_to_ad(bs_date_str)

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

    def _flag_truncated_roster(
        self, court_case: dict, case: Case, parsed_count: int, bound_count: int
    ) -> None:
        """Flag the case when NGM's parsed defendant list is truncated.

        NGM's Special-Court defendant parse is frequently incomplete (capped, or
        only the lead defendant), while the court's own summary cell states the
        true total as ``"<lead> समेत N"`` or ends in a bare ``"समेत"``. When the
        stated total exceeds the number NGM parsed — or the cell is a bare
        ``"समेत"`` — the accused list is incomplete and the case must be rebuilt
        from the court order before it is published, so we record that in
        ``missing_details`` (the review flow surfaces it).

        The truncation decision compares against ``parsed_count`` (what NGM
        actually parsed), NOT ``bound_count`` (which further drops defendants
        lacking a NES id) — otherwise a complete roster with a few unresolved
        ids would be mislabelled as source-truncated. Advisory only: if the NGM
        read/parse fails, the check is skipped, never fatal to the import.
        """
        court = court_case.get("court")
        case_no = court_case.get("case_no")
        if not (court and case_no):
            return
        # Local import keeps the NGM read plane off this module's import graph.
        from cases.services.ngm_court_records import get_court_case_details

        try:
            details = get_court_case_details(court, case_no)
            cell = (details or {}).get("case", {}).get("defendant")
            stated, bare = parse_stated_defendant_count(cell)
        except Exception:  # noqa: BLE001 - defensive: a roster-flag failure must not fail the draft
            logger.warning(
                "Truncation guard skipped: NGM details unavailable for %s/%s",
                court,
                case_no,
                exc_info=True,
            )
            return
        if not ((stated is not None and stated > parsed_count) or bare):
            return
        expected = f"≈{stated}" if stated is not None else "समेत (unknown total)"
        note = (
            f"ACCUSED LIST INCOMPLETE: {bound_count} defendant(s) imported "
            f"(NGM parsed {parsed_count}); court record states {expected}. "
            "Roster truncated at source — rebuild from the court order before "
            "publishing."
        )
        existing = (case.missing_details or "").strip()
        case.missing_details = f"{existing}\n{note}".strip() if existing else note
        case.save(update_fields=["missing_details"])
        self.stats["cases_flagged_truncated"] += 1

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
