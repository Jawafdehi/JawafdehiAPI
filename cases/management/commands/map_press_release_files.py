"""
Django management command to map CIAA press release web URLs to actual document files.

Maps CIAA press release web page URLs (e.g., https://ciaa.gov.np/pressrelease/3345)
to actual PDF/DOCX file URLs from the NGM bucket. Creates DocumentSource records
for each file and updates case evidence accordingly.

Usage:
    python manage.py map_press_release_files --dry-run  # Test first
    python manage.py map_press_release_files            # Apply changes
    python manage.py map_press_release_files --case-id=case-abc123  # Specific case
    python manage.py map_press_release_files --limit=10  # Process first 10 cases
"""

from __future__ import annotations

import logging
from typing import Optional

import requests
from django.core.management.base import BaseCommand

logger = logging.getLogger(__name__)

# NGM root index URL (always fetches latest dated index)
NGM_ROOT_INDEX_URL = "https://ngm-store.jawafdehi.org/index-v2.json"


class Command(BaseCommand):
    help = "Map CIAA press release web URLs to actual document files from NGM bucket"

    def __init__(self):
        super().__init__()
        self.press_release_index = {}
        self.stats = {
            "cases_processed": 0,
            "cases_fixed": 0,
            "cases_skipped": 0,
            "sources_created": 0,
            "sources_updated": 0,
            "evidence_updated": 0,
            "errors": 0,
        }

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run in dry-run mode (no database changes)",
        )
        parser.add_argument(
            "--case-id",
            type=str,
            help="Fix specific case by slug (optional)",
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Limit number of cases to process (optional)",
        )

    def handle(self, *args, **options):
        raise NotImplementedError(
            "This command creates/reads DocumentSource rows, which have been "
            "removed (ADR: cases own no documents). It must be rewired to create "
            "Material + CaseMaterialReference records before use. See "
            "docs/jawafdehi/sources-to-materials-prod-migration.md."
        )

    def load_press_release_index(self) -> bool:
        """Load press release index from NGM bucket with pagination support."""
        try:
            # Get root index to find latest press release index URL
            self.stdout.write(f"Loading root index from {NGM_ROOT_INDEX_URL}...")
            response = requests.get(NGM_ROOT_INDEX_URL, timeout=30)
            response.raise_for_status()
            root_data = response.json()

            # Find ciaa-press-releases child and get its $ref URL
            press_release_url = None
            for child in root_data.get("children", []):
                if child.get("name") == "ciaa-press-releases":
                    press_release_url = child.get("$ref")
                    break

            if not press_release_url:
                self.stdout.write(
                    self.style.ERROR("Could not find ciaa-press-releases in root index")
                )
                return False

            self.stdout.write(f"Found press release index: {press_release_url}")

            # Load all pages of press releases
            current_url = press_release_url
            page_num = 1

            while current_url:
                self.stdout.write(f"  Loading page {page_num}...")
                response = requests.get(current_url, timeout=30)
                response.raise_for_status()
                data = response.json()

                # Build index: press_id -> press release data with files
                for manuscript in data.get("manuscripts", []):
                    metadata = manuscript.get("metadata", {})
                    press_id = metadata.get("press_id")
                    if press_id:
                        if press_id not in self.press_release_index:
                            self.press_release_index[press_id] = {
                                "source_url": metadata.get("source_url"),
                                "title": metadata.get("title"),
                                "publication_date": metadata.get("publication_date"),
                                "files": [],
                            }
                        self.press_release_index[press_id]["files"].append(
                            {
                                "url": manuscript.get("url"),
                                "file_name": manuscript.get("file_name"),
                            }
                        )

                # Check for next page
                current_url = data.get("next")
                if current_url:
                    page_num += 1

            self.stdout.write(
                self.style.SUCCESS(
                    f"✓ Loaded {len(self.press_release_index)} press releases from {page_num} page(s)"
                )
            )
            return True

        except Exception as e:
            logger.exception(f"Failed to load press release index: {e}")
            self.stdout.write(self.style.ERROR(f"Failed to load index: {e}"))
            return False

    def extract_press_id(self, url: str) -> Optional[int]:
        """Extract press_id from CIAA press release URL."""
        try:
            # URL format: https://ciaa.gov.np/pressrelease/3345
            parts = url.rstrip("/").split("/")
            return int(parts[-1])
        except (ValueError, IndexError):
            return None

    def print_summary(self, dry_run: bool):
        """Print summary statistics."""
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(
            self.style.WARNING(f"{'[DRY RUN] ' if dry_run else ''}SUMMARY")
        )
        self.stdout.write("=" * 60)
        self.stdout.write(f"Cases processed:     {self.stats['cases_processed']}")
        self.stdout.write(
            self.style.SUCCESS(f"✓ Cases mapped:      {self.stats['cases_fixed']}")
        )
        self.stdout.write(
            self.style.WARNING(f"⊘ Cases skipped:     {self.stats['cases_skipped']}")
        )
        self.stdout.write(f"Sources created:     {self.stats['sources_created']}")
        self.stdout.write(f"Sources updated:     {self.stats['sources_updated']}")
        self.stdout.write(f"Evidence updated:    {self.stats['evidence_updated']}")
        if self.stats["errors"] > 0:
            self.stdout.write(
                self.style.ERROR(f"✗ Errors:            {self.stats['errors']}")
            )
        self.stdout.write("=" * 60)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "\nThis was a dry run. No changes were made to the database."
                )
            )
            self.stdout.write("Run without --dry-run to apply changes.")
