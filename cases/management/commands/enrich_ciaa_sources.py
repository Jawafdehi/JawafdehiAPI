"""
Django management command to discover and attach additional document sources
to CIAA FY 080/081 draft cases.

Matches court case numbers against pre-built indices (ag_index.csv) for
AG charge sheets, and enriches existing CIAA press release sources with
metadata from ciaa-press-releases.csv.

Usage:
    python manage.py enrich_ciaa_sources --dry-run
    python manage.py enrich_ciaa_sources --limit 10
    python manage.py enrich_ciaa_sources --case-id case-abc123
"""

import csv
import logging
from datetime import date
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from nepali.datetime import nepalidate

from cases.models import (
    Case,
    CaseEntityRelationship,
    DocumentSource,
    RelationshipType,
    SourceType,
)

logger = logging.getLogger(__name__)

DEFAULT_DATA_DIR = (
    Path(settings.BASE_DIR)
    / "case_workflows"
    / "workflows"
    / "ciaa_caseworker"
    / "data"
)


class Command(BaseCommand):
    help = "Discover and attach additional document sources to CIAA draft cases"

    def __init__(self):
        super().__init__()
        self.ag_index = {}
        self.press_release_index: dict[str, dict] = {}
        self.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "ag_sources_created": 0,
            "ag_sources_skipped": 0,
            "pr_sources_enriched": 0,
            "pr_sources_skipped": 0,
            "evidence_updated": 0,
            "errors": 0,
        }

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview without saving any changes",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Process only N cases",
        )
        parser.add_argument(
            "--case-id",
            type=str,
            help="Process a single case by case_id",
        )
        parser.add_argument(
            "--data-dir",
            type=str,
            default=str(DEFAULT_DATA_DIR),
            help=f"Directory containing ag_index.csv and ciaa-press-releases.csv (default: {DEFAULT_DATA_DIR})",
        )
        parser.add_argument(
            "--skip-press-releases",
            action="store_true",
            help="Skip CIAA press release enrichment (AG charge sheets only)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options.get("limit")
        case_id = options.get("case_id")
        data_dir = Path(options["data_dir"])
        skip_pr = options["skip_press_releases"]

        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write(
            self.style.WARNING(f"{prefix}Starting CIAA source enrichment...")
        )

        if not self._load_data_indices(data_dir):
            self.stdout.write(
                self.style.ERROR("Failed to load data indices. Aborting.")
            )
            return

        cases = self._get_cases(case_id, limit)
        self.stdout.write(f"Found {len(cases)} DRAFT case(s) to process")

        for case in cases:
            try:
                self._process_case(case, dry_run, skip_pr)
            except Exception:
                self.stats["errors"] += 1
                logger.exception(f"Error processing case {case.case_id}")

        self._print_summary(dry_run)

    def _load_data_indices(self, data_dir: Path) -> bool:
        ag_path = data_dir / "ag_index.csv"
        pr_path = data_dir / "ciaa-press-releases.csv"

        if not ag_path.exists():
            self.stdout.write(
                self.style.ERROR(f"AG index not found: {ag_path}")
            )
            return False

        if not pr_path.exists():
            self.stdout.write(
                self.style.ERROR(f"Press release index not found: {pr_path}")
            )
            return False

        self._load_ag_index(ag_path)
        self._load_press_release_index(pr_path)
        return True

    def _load_ag_index(self, path: Path):
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                case_number = (row.get("case_number") or "").strip().upper()
                if case_number:
                    self.ag_index[case_number] = {
                        "title": (row.get("title") or "").strip(),
                        "filing_date": (row.get("filing_date") or "").strip(),
                        "pdf_url": (row.get("pdf_url") or "").strip(),
                        "court_office": (row.get("court_office") or "").strip(),
                    }

        self.stdout.write(
            self.style.SUCCESS(
                f"Loaded {len(self.ag_index)} AG charge sheet entries"
            )
        )

    def _load_press_release_index(self, path: Path):
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                source_url = (row.get("source_url") or "").strip()
                press_id = (row.get("press_id") or "").strip()
                if source_url:
                    key = source_url
                    if key not in self.press_release_index:
                        self.press_release_index[key] = {
                            "press_id": press_id,
                            "publication_date": (row.get("publication_date") or "").strip(),
                            "title": (row.get("title") or "").strip(),
                            "source_url": source_url,
                        }

        self.stdout.write(
            self.style.SUCCESS(
                f"Loaded {len(self.press_release_index)} unique press release entries"
            )
        )

    def _get_cases(
        self, case_id: Optional[str], limit: Optional[int]
    ) -> list[Case]:
        queryset = Case.objects.filter(state="DRAFT")

        if case_id:
            queryset = queryset.filter(case_id=case_id)

        cases = list(queryset)

        if limit and limit < len(cases):
            cases = cases[:limit]

        return cases

    def _process_case(self, case: Case, dry_run: bool, skip_pr: bool):
        self.stats["cases_processed"] += 1
        case_no_display = self._extract_case_number(case) or case.case_id
        self.stdout.write(
            f"\n[{self.stats['cases_processed']}] {case.case_id} ({case_no_display})"
        )

        enriched = False

        if self._enrich_ag_charge_sheet(case, dry_run):
            enriched = True

        if not skip_pr and self._enrich_press_releases(case, dry_run):
            enriched = True

        if enriched:
            self.stats["cases_enriched"] += 1
        else:
            self.stats["cases_skipped"] += 1
            self.stdout.write(self.style.WARNING("  No new sources to attach"))

    def _extract_case_number(self, case: Case) -> Optional[str]:
        if not case.court_cases:
            return None
        for ref in case.court_cases:
            if not isinstance(ref, str) or ":" not in ref:
                continue
            _, case_number = ref.split(":", 1)
            if case_number.strip():
                return case_number.strip().upper()
        return None

    def _enrich_ag_charge_sheet(self, case: Case, dry_run: bool) -> bool:
        case_number = self._extract_case_number(case)
        if not case_number:
            return False

        ag_entry = self.ag_index.get(case_number)
        if not ag_entry:
            return False

        pdf_url = ag_entry["pdf_url"]
        if not pdf_url:
            return False

        if self._url_already_in_evidence(case, pdf_url):
            self.stats["ag_sources_skipped"] += 1
            self.stdout.write(f"  AG charge sheet already attached for {case_number}")
            return False

        title = ag_entry["title"] or f"AG Charge Sheet - {case_number}"
        filing_date = ag_entry["filing_date"]

        pub_date = self._parse_bs_date(filing_date)

        if dry_run:
            self.stats["ag_sources_created"] += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"  [DRY RUN] Would create AG charge sheet source: {title[:80]}"
                )
            )
            self.stdout.write(f"    URL: {pdf_url}")
            if pub_date:
                self.stdout.write(f"    Publication date: {pub_date}")
            return True

        try:
            with transaction.atomic():
                source = DocumentSource.objects.create(
                    title=title[:300],
                    description=f"AG Charge Sheet for case {case_number} from {ag_entry.get('court_office', '')}"[
                        :500
                    ],
                    source_type=SourceType.LEGAL_PROCEDURAL,
                    url=[pdf_url],
                    publication_date=pub_date,
                )

                evidence = list(case.evidence) if case.evidence else []
                evidence.append(
                    {
                        "source_id": source.source_id,
                        "description": f"AG Charge Sheet - {case_number}",
                    }
                )
                case.evidence = evidence
                case.save(update_fields=["evidence"])

            self.stats["ag_sources_created"] += 1
            self.stats["evidence_updated"] += 1
            self.stdout.write(
                self.style.SUCCESS(
                    f"  Created AG charge sheet source: {source.source_id}"
                )
            )
            return True

        except Exception:
            logger.exception(
                f"Failed to create AG charge sheet source for case {case.case_id}"
            )
            self.stats["errors"] += 1
            return False

    def _enrich_press_releases(self, case: Case, dry_run: bool) -> bool:
        enriched = False

        defendants = self._get_defendant_names(case)
        existing_sources = self._get_existing_sources_for_case(case)

        for source_id, source in existing_sources.items():
            if not isinstance(source.url, list):
                continue

            for url in source.url:
                if "ciaa.gov.np/pressrelease/" not in url:
                    continue

                pr_data = self.press_release_index.get(url)
                if not pr_data:
                    continue

                if self._enrich_existing_pr_source(
                    source, pr_data, dry_run
                ):
                    self.stats["pr_sources_enriched"] += 1
                    enriched = True
                else:
                    self.stats["pr_sources_skipped"] += 1

                break

        if defendants:
            for source_url, pr_data in self.press_release_index.items():
                if self._url_already_in_evidence(case, source_url):
                    continue

                pr_title = pr_data.get("title", "")
                if not self._title_matches_defendant(pr_title, defendants):
                    continue

                if dry_run:
                    self.stats["pr_sources_enriched"] += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  [DRY RUN] Would create press release source "
                            f"matching defendant: {pr_title[:80]}"
                        )
                    )
                    enriched = True
                    continue

                try:
                    with transaction.atomic():
                        pub_date = self._parse_bs_date(
                            pr_data.get("publication_date", "")
                        )
                        source = DocumentSource.objects.create(
                            title=pr_title[:300],
                            description=f"CIAA Press Release (press_id: {pr_data.get('press_id', 'N/A')})"[
                                :500
                            ],
                            source_type=SourceType.LEGAL_PROCEDURAL,
                            url=[source_url],
                            publication_date=pub_date,
                        )

                        evidence = list(case.evidence) if case.evidence else []
                        evidence.append(
                            {
                                "source_id": source.source_id,
                                "description": f"CIAA Press Release - {pr_data.get('press_id', 'N/A')}",
                            }
                        )
                        case.evidence = evidence
                        case.save(update_fields=["evidence"])

                    self.stats["pr_sources_enriched"] += 1
                    self.stats["evidence_updated"] += 1
                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  Created press release source: {source.source_id}"
                        )
                    )
                    enriched = True

                except Exception:
                    logger.exception(
                        f"Failed to create press release source for case {case.case_id}"
                    )
                    self.stats["errors"] += 1

        return enriched

    def _get_defendant_names(self, case: Case) -> list[str]:
        entities = CaseEntityRelationship.objects.filter(
            case=case, relationship_type=RelationshipType.ACCUSED
        ).select_related("entity")

        names = []
        for rel in entities:
            name = (rel.entity.display_name or "").strip()
            if name:
                names.append(name)
        return names

    def _get_existing_sources_for_case(
        self, case: Case
    ) -> dict[str, DocumentSource]:
        if not case.evidence:
            return {}

        source_ids = [
            e.get("source_id")
            for e in case.evidence
            if e.get("source_id")
        ]
        if not source_ids:
            return {}

        return {
            s.source_id: s
            for s in DocumentSource.objects.filter(
                source_id__in=source_ids, is_deleted=False
            )
        }

    def _url_already_in_evidence(self, case: Case, url: str) -> bool:
        if not url or not case.evidence:
            return False

        source_ids = {
            e.get("source_id")
            for e in case.evidence
            if e.get("source_id")
        }
        if not source_ids:
            return False

        if connection.vendor == "postgresql":
            return DocumentSource.objects.filter(
                source_id__in=source_ids,
                is_deleted=False,
                url__contains=[url],
            ).exists()
        else:
            sources = DocumentSource.objects.filter(
                source_id__in=source_ids, is_deleted=False
            )
            for source in sources:
                if (
                    isinstance(source.url, list)
                    and url in source.url
                ):
                    return True
            return False

    def _title_matches_defendant(
        self, title: str, defendants: list[str]
    ) -> bool:
        if not title:
            return False
        for defendant in defendants:
            if len(defendant) >= 3 and defendant in title:
                return True
        return False

    def _enrich_existing_pr_source(
        self,
        source: DocumentSource,
        pr_data: dict,
        dry_run: bool,
    ) -> bool:
        needs_update = False
        update_fields = []

        if not source.publication_date and pr_data.get("publication_date"):
            pub_date = self._parse_bs_date(pr_data["publication_date"])
            if pub_date:
                needs_update = True
                source.publication_date = pub_date
                update_fields.append("publication_date")

        if (
            pr_data.get("title")
            and source.title == "CIAA Press Release"
            and pr_data["title"][:300] != source.title
        ):
            needs_update = True
            source.title = pr_data["title"][:300]
            update_fields.append("title")

        if not needs_update:
            return False

        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    f"  [DRY RUN] Would enrich existing source "
                    f"{source.source_id} ({', '.join(update_fields)})"
                )
            )
            return True

        try:
            source.save(update_fields=update_fields)
            self.stdout.write(
                self.style.SUCCESS(
                    f"  Enriched existing source {source.source_id} "
                    f"({', '.join(update_fields)})"
                )
            )
            return True
        except Exception:
            logger.exception(
                f"Failed to enrich source {source.source_id}"
            )
            return False

    def _parse_bs_date(self, date_str: str) -> Optional[date]:
        if not date_str:
            return None
        try:
            parts = date_str.strip().split("-")
            if len(parts) != 3:
                return None
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
            return nepalidate(year, month, day).to_datetime().date()
        except Exception:
            return None

    def _print_summary(self, dry_run: bool):
        prefix = "[DRY RUN] " if dry_run else ""
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(self.style.WARNING(f"{prefix}SUMMARY"))
        self.stdout.write("=" * 60)
        self.stdout.write(f"Cases processed:     {self.stats['cases_processed']}")
        self.stdout.write(
            self.style.SUCCESS(f"Cases enriched:     {self.stats['cases_enriched']}")
        )
        self.stdout.write(
            self.style.WARNING(f"Cases skipped:      {self.stats['cases_skipped']}")
        )
        self.stdout.write(f"AG sources created: {self.stats['ag_sources_created']}")
        self.stdout.write(f"AG sources skipped: {self.stats['ag_sources_skipped']}")
        self.stdout.write(f"PR sources enriched:{self.stats['pr_sources_enriched']}")
        self.stdout.write(f"PR sources skipped: {self.stats['pr_sources_skipped']}")
        self.stdout.write(f"Evidence updated:   {self.stats['evidence_updated']}")
        if self.stats["errors"] > 0:
            self.stdout.write(
                self.style.ERROR(f"Errors:             {self.stats['errors']}")
            )
        self.stdout.write("=" * 60)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "\nThis was a dry run. No changes were made to the database."
                )
            )
            self.stdout.write("Run without --dry-run to apply changes.")
