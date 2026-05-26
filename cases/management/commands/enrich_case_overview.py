"""
Enrich DRAFT CIAA cases with an LLM-generated case overview from evidence content.

Consolidated pipeline: evidence gathering, likhit conversion, section generation
(core + conditional court-stage), assembly, and save to Case model.

Usage::

    python manage.py enrich_case_overview --dry-run
    python manage.py enrich_case_overview --limit 10
    python manage.py enrich_case_overview --case-id case-078-CR-0123 --verbose

Environment variables::

    JAWAFDEHI_LLM_API_KEY    — API key for Jawafdehi LLM proxy
    JAWAFDEHI_LLM_PROXY_URL  — base URL for Jawafdehi LLM proxy
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
import urllib.parse
import urllib.request
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from cases.management.commands._enrich_utils import (
    build_pinned_opener,
    confined_output_path,
    copy_stream_to_path_with_limit,
    sanitize_download_filename,
    validate_host_safety,
)
from cases.models import Case, CaseState, DocumentSource, SourceType
from cases.services.likhit_util import (
    ConversionResult,
    convert_bytes_to_markdown,
    evidence_content_hash,
)
from cases.services.section_generation import (
    ALL_SECTION_KEYS,
    CORE_SECTION_KEYS,
    COURT_STAGE_KEYS,
    SECTION_ORDER,
    SECTION_SPECS,
    SectionEvidence,
    SectionGenerationResult,
    SectionGenerationService,
    SectionLLMClient,
    SectionQualityError,
    build_readiness_check,
    extract_case_evidence,
)
from caseworker.services import LLMService

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger(__name__)


class DjangoSyncLLMClient:
    """LLM client using the DB-configured LLM provider (synchronous)."""

    def __init__(self):
        self.service = LLMService()

    async def generate(
        self, *, system_prompt: str, user_prompt: str, max_tokens: int
    ) -> str:
        from asgiref.sync import sync_to_async

        def call():
            prompt = f"{system_prompt}\n\n{user_prompt}"
            text = self.service.invoke(prompt)
            json.loads(text)
            return text

        return await sync_to_async(call)()


class Command(BaseCommand):
    help = "Generate case overview from CIAA case evidence content using LLM"

    def __init__(self):
        super().__init__()
        self.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_failed": 0,
            "cases_no_content": 0,
        }

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview without saving to database",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Process only N cases (useful for testing)",
        )
        parser.add_argument(
            "--case-id",
            type=str,
            default=None,
            help="Process a specific case by case_id",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable detailed debug logging",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-process cases that already have an overview",
        )
        parser.add_argument(
            "--core-only",
            action="store_true",
            help="Generate only core sections (क, ख, ग), skip conditional court-stage sections",
        )
        parser.add_argument(
            "--show-readiness",
            action="store_true",
            help="Print section readiness report and exit without generating",
        )
        parser.add_argument(
            "--skip-evidence-conversion",
            action="store_true",
            help="Skip MarkItDown evidence file conversion, use metadata only",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        case_id = options.get("case_id")
        verbose = options["verbose"]
        force = options["force"]
        core_only = options["core_only"]
        show_readiness = options["show_readiness"]
        skip_conversion = options["skip_evidence_conversion"]

        if verbose:
            logger.setLevel(logging.DEBUG)

        self.stdout.write(
            self.style.WARNING(
                f"{'[DRY RUN] ' if dry_run else ''}"
                "Starting case overview enrichment..."
            )
        )

        cases = self._get_eligible_cases(limit, case_id, force)

        if not cases:
            self.stdout.write("No eligible cases found.")
            return

        self.stdout.write(f"Found {len(cases)} eligible CIAA DRAFT case(s) to process")

        # Pre-fetch document sources
        self._source_lookup = self._build_source_lookup(cases)

        for idx, case in enumerate(cases, 1):
            try:
                self.stdout.write(
                    f"\n[{idx}/{len(cases)}] {case.case_id} - {case.title[:80]}..."
                )
                self._process_case(
                    case,
                    dry_run=dry_run,
                    core_only=core_only,
                    show_readiness=show_readiness,
                    skip_conversion=skip_conversion,
                )
            except Exception as e:
                self.stats["cases_failed"] += 1
                logger.exception(f"Error processing {case.case_id}: {e}")
                self.stdout.write(self.style.ERROR(f"FAILED: {case.case_id} - {e}"))

        self._print_summary(dry_run)

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def _get_eligible_cases(self, limit, case_id, force=False):
        queryset = Case.objects.filter(state=CaseState.DRAFT)

        if case_id:
            queryset = queryset.filter(case_id=case_id)

        if not force:
            queryset = queryset.filter(
                short_description__isnull=True,
            ) | queryset.filter(short_description="")

        if limit is not None:
            if limit < 0:
                raise CommandError(f"--limit must be >= 0, got {limit}")
            if limit > 0:
                queryset = queryset[:limit]
            else:
                return []

        return list(queryset)

    def _build_source_lookup(self, cases):
        source_ids = set()
        for case in cases:
            if not case.evidence or not isinstance(case.evidence, (list, tuple)):
                continue
            for entry in case.evidence:
                if not isinstance(entry, dict):
                    continue
                if isinstance((sid := entry.get("source_id")), str) and sid.strip():
                    source_ids.add(sid)

        return {
            source.source_id: source
            for source in DocumentSource.objects.filter(
                source_id__in=source_ids, is_deleted=False
            ).prefetch_related("uploaded_files")
        }

    # ------------------------------------------------------------------
    # Per-case processing
    # ------------------------------------------------------------------

    def _process_case(
        self,
        case,
        *,
        dry_run=False,
        core_only=False,
        show_readiness=False,
        skip_conversion=False,
    ):
        self.stats["cases_processed"] += 1

        if not case.evidence:
            self.stats["cases_skipped"] += 1
            self.stdout.write(self.style.WARNING("  SKIPPED: No evidence"))
            return

        # Step 1: Gather evidence text (metadata + optional file conversion)
        if not skip_conversion:
            evidence_items = self._gather_evidence_with_conversion(case)
        else:
            evidence_items = extract_case_evidence(case)

        if not evidence_items:
            self.stats["cases_no_content"] += 1
            self.stdout.write(self.style.WARNING("  SKIPPED: No evidence after gathering"))
            return

        total_chars = sum(len(e.text) for e in evidence_items)
        self.stdout.write(f"  Evidence: {len(evidence_items)} sources ({total_chars} chars)")

        # Step 2: Section readiness
        readiness = build_readiness_check(case, evidence_items)

        if show_readiness:
            self._print_readiness_report(readiness)
            return

        # Step 3: Generate sections
        service = SectionGenerationService(DjangoSyncLLMClient())

        if core_only:
            results = asyncio.run(
                service.generate_core_sections(
                    case, evidence_items, section_keys=CORE_SECTION_KEYS
                )
            )
        else:
            results = asyncio.run(
                service.generate_all_sections(
                    case, evidence_items, include_conditional=True
                )
            )

        # Step 4: Validate and count
        active_keys = list(results.keys())
        cache_hits = sum(1 for r in results.values() if r.from_cache)
        self.stdout.write(
            f"  Generated {len(active_keys)} sections "
            f"({cache_hits} cache hits) "
            f"core={len([k for k in CORE_SECTION_KEYS if k in results])} "
            f"court={len([k for k in COURT_STAGE_KEYS if k in results])}"
        )

        # Step 5: Assembly & save
        if dry_run:
            for key in SECTION_ORDER:
                if key in results:
                    self.stdout.write(f"\n  --- {SECTION_SPECS[key].title} ---")
                    self.stdout.write(f"  Confidence: {results[key].confidence}")
                    self.stdout.write(results[key].html[:200] + "...")
            self.stdout.write(
                self.style.SUCCESS("  [DRY RUN] Would save assembled overview")
            )
            self.stats["cases_enriched"] += 1
            return

        self._assemble_and_save(case, results)
        self.stats["cases_enriched"] += 1
        self.stdout.write(self.style.SUCCESS(f"  SAVED: {case.case_id}"))

    # ------------------------------------------------------------------
    # Evidence gathering with Likhit conversion
    # ------------------------------------------------------------------

    def _gather_evidence_with_conversion(self, case) -> list[SectionEvidence]:
        source_ids = [
            item.get("source_id")
            for item in case.evidence or []
            if item.get("source_id")
        ]
        sources = [
            s for sid in source_ids if (s := self._source_lookup.get(sid)) is not None
        ]
        if not sources:
            return []

        evidence_items: list[SectionEvidence] = []
        for source in sources:
            text_parts = [source.title or "", source.description or ""]
            converted = self._convert_source(source)
            if converted:
                text_parts.insert(0, converted)

            for upload in source.uploaded_files.all():
                if upload.filename:
                    text_parts.append(upload.filename)

            text = "\n".join(p for p in text_parts if p)
            if text.strip():
                evidence_items.append(
                    SectionEvidence(
                        source_id=source.source_id,
                        title=source.title,
                        source_type=source.source_type,
                        text=text,
                    )
                )

        return evidence_items

    def _convert_source(self, source: DocumentSource) -> str | None:
        """Convert source files to markdown via Likhit/MarkItDown, return text or None."""
        results: list[str] = []

        # Try uploaded file
        if source.uploaded_file:
            try:
                with source.uploaded_file.open("rb") as f:
                    content = f.read()
                result = convert_bytes_to_markdown(
                    content,
                    filename=source.uploaded_filename or source.uploaded_file.name,
                )
                if result.markdown and len(result.markdown.strip()) >= 50:
                    results.append(result.markdown)
            except Exception as e:
                logger.debug("Uploaded file conversion failed for %s: %s", source.source_id, e)

        # Try uploaded_files (DocumentSourceUpload)
        for upload in source.uploaded_files.all():
            if not upload.file:
                continue
            try:
                with upload.file.open("rb") as f:
                    content = f.read()
                result = convert_bytes_to_markdown(
                    content,
                    filename=upload.filename or upload.file.name,
                )
                if result.markdown and len(result.markdown.strip()) >= 50:
                    results.append(result.markdown)
            except Exception as e:
                logger.debug("Upload conversion failed for %s: %s", source.source_id, e)

        # Try URLs
        for url in self._ranked_source_urls(source):
            try:
                text = self._download_url_text(url, source.source_id)
                if text:
                    results.append(text)
            except Exception as e:
                logger.debug("URL download failed for %s: %s", source.source_id, e)

        return "\n\n".join(results) if results else None

    def _ranked_source_urls(self, source: DocumentSource) -> list[str]:
        urls = [
            url.strip()
            for url in (source.url or [])
            if isinstance(url, str) and url.strip()
        ]
        if not urls:
            return []

        direct_urls = [u for u in urls if self._is_direct_document_url(u)]
        non_direct = [u for u in urls if u not in direct_urls]

        def priority(url):
            parsed = urllib.parse.urlparse(url)
            host = parsed.netloc.lower()
            path = urllib.parse.unquote(parsed.path).lower()
            return (int(host == "ngm-store.jawafdehi.org"), int(path.endswith(".pdf")))

        direct_urls.sort(key=priority, reverse=True)
        return direct_urls + non_direct

    def _is_direct_document_url(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path).lower()
        return path.endswith((".pdf", ".doc", ".docx"))

    def _download_url_text(self, url: str, source_id: str) -> str | None:
        self._validate_url_scheme(url)
        parsed = urllib.parse.urlparse(url)
        guessed_name = sanitize_download_filename(parsed.path, source_id)

        with tempfile.TemporaryDirectory(prefix="overview-enrich-") as tmp_dir:
            tmp_dir_path = Path(tmp_dir)
            out_path = confined_output_path(tmp_dir_path, guessed_name)
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/146.0.0.0 Safari/537.36"
                        )
                    },
                )
                pinned_addrs = validate_host_safety(parsed.hostname, parsed.port or 0)
                opener = build_pinned_opener(url, pinned_addrs)
                with opener.open(request, timeout=30) as response:
                    copy_stream_to_path_with_limit(response, out_path)
            except OSError:
                out_path.unlink(missing_ok=True)
                return None
            except CommandError:
                out_path.unlink(missing_ok=True)
                raise

            try:
                result = convert_bytes_to_markdown(
                    out_path.read_bytes(), filename=out_path.name
                )
                if result.markdown and len(result.markdown.strip()) >= 50:
                    return result.markdown
            except Exception:
                return None
            return None

    def _validate_url_scheme(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            validate_host_safety(parsed.hostname, parsed.port or 0)
            return url
        raise ValueError(
            f"Invalid URL '{url}'. Only http and https URLs are allowed with a host."
        )

    # ------------------------------------------------------------------
    # Assembly & save
    # ------------------------------------------------------------------

    def _assemble_and_save(
        self, case: Case, results: dict[str, SectionGenerationResult]
    ) -> None:
        """Assemble sections in fixed order and save to Case."""
        if "short_description" in results:
            case.short_description = results["short_description"].html

        html_parts: list[str] = []
        for key in SECTION_ORDER:
            if key in results and key != "short_description":
                html_parts.append(results[key].html)

        if html_parts:
            case.description = "\n\n".join(html_parts)

        case.save(update_fields=["short_description", "description", "updated_at"])

    # ------------------------------------------------------------------
    # Reports
    # ------------------------------------------------------------------

    def _print_readiness_report(self, readiness):
        self.stdout.write(self.style.SUCCESS("Section Readiness Report"))
        self.stdout.write(f"  court_cases: {readiness.court_cases or '[]'}")
        self.stdout.write(
            f"  evidence text length: {len(readiness.evidence_text)} chars"
        )
        self.stdout.write("")
        for key in ALL_SECTION_KEYS:
            result = readiness.check_section(key)
            status = (
                self.style.SUCCESS("ACTIVE  ")
                if result.active
                else self.style.WARNING("INACTIVE")
            )
            stage = f" [{result.court_stage.value}]" if result.court_stage else ""
            self.stdout.write(f"  {status} {key}{stage} — {result.reason}")

    def _print_summary(self, dry_run):
        self.stdout.write("\n" + "=" * 60)
        self.stdout.write(
            self.style.WARNING(f"{'[DRY RUN] ' if dry_run else ''}SUMMARY")
        )
        self.stdout.write("=" * 60)
        self.stdout.write(f"Cases processed:  {self.stats['cases_processed']}")
        self.stdout.write(
            self.style.SUCCESS(f"Cases enriched:   {self.stats['cases_enriched']}")
        )
        self.stdout.write(
            self.style.WARNING(f"Cases skipped:    {self.stats['cases_skipped']}")
        )
        self.stdout.write(
            self.style.WARNING(
                f"Cases no content:  {self.stats['cases_no_content']}"
            )
        )
        if self.stats["cases_failed"] > 0:
            self.stdout.write(
                self.style.ERROR(f"Cases failed:     {self.stats['cases_failed']}")
            )
        self.stdout.write("=" * 60)

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    "\nThis was a dry run. No changes were made to the database."
                )
            )
            self.stdout.write("Run without --dry-run to apply changes.")
