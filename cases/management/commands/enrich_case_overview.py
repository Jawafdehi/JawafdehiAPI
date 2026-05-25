"""
Enrich DRAFT CIAA cases with an LLM-generated case overview from press release content.

Phase 1: management command skeleton, likhit conversion, evidence gathering & routing.
Phase 2+: LLM extraction and save to Case model.

Usage::

    python manage.py enrich_case_overview --dry-run
    python manage.py enrich_case_overview --limit 10
    python manage.py enrich_case_overview --case-id case-078-CR-0123 --verbose

Environment variables::

    JAWAFDEHI_LLM_API_KEY    — API key for Jawafdehi LLM proxy
    JAWAFDEHI_LLM_PROXY_URL  — base URL for Jawafdehi LLM proxy
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from cases.models import Case, CaseState, DocumentSource, SourceType
from cases.services.priority_case_loader import filter_by_priority, load_priority_cases

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 16 * 1024

# SSRF protection: block well-known internal/metadata endpoints.
_CLOUD_METADATA_IP = "169.254.169.254"  # NOSONAR — link-local, not configurable

_SSRF_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        _CLOUD_METADATA_IP,
        "metadata",
        "0.0.0.0",
    }
)


def _validate_host_safety(hostname: str) -> None:
    host = hostname.lower().rstrip(".")
    if host in _SSRF_BLOCKED_HOSTNAMES:
        raise ValueError(
            f"Blocked internal host: {hostname!r}. "
            "Download sources must target public hosts only."
        )
    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(
            f"Cannot resolve host: {hostname!r}. "
            "Only resolvable public hosts are allowed for source downloads."
        ) from exc
    for info in addrinfo:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
            raise ValueError(
                f"Blocked internal address: {hostname!r} → {addr}. "
                "Download sources must target public IPs only."
            )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                f"Unsafe redirect scheme/host to {newurl}",
                headers,
                fp,
            )
        _validate_host_safety(parsed.hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _sanitize_download_filename(filename: str | None, source_id: str) -> str:
    raw = (filename or "").strip()
    if not raw:
        return f"{source_id}.bin"

    decoded = urllib.parse.unquote(raw)
    candidate = Path(decoded).name.strip()

    if candidate in {"", ".", ".."}:
        return f"{source_id}.bin"

    candidate = candidate.replace("\x00", "")
    candidate = re.sub(r"[<>:\"/\\|?*]+", "_", candidate).rstrip(" .")
    if candidate in {"", ".", ".."}:
        return f"{source_id}.bin"

    max_len = 200
    if len(candidate) <= max_len:
        return candidate

    suffix = "".join(Path(candidate).suffixes)
    stem = candidate[: -len(suffix)] if suffix else candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:10]

    stem_budget = max_len - len(suffix) - len(digest) - 1
    if stem_budget < 1:
        return f"{source_id}-{digest}{suffix}"[:max_len]

    truncated_stem = stem[:stem_budget].rstrip(" .-_")
    if not truncated_stem:
        truncated_stem = source_id

    return f"{truncated_stem}-{digest}{suffix}"


def _confined_output_path(output_dir: Path, filename: str) -> Path:
    output_dir_resolved = output_dir.resolve()
    out_path = (output_dir / filename).resolve()
    if output_dir_resolved not in out_path.parents:
        raise CommandError(f"Refusing to write outside output directory: '{filename}'")
    return out_path


def _copy_stream_to_path_with_limit(in_file: Any, out_path: Path) -> None:
    total_bytes = 0
    try:
        with out_path.open("wb") as out_file:
            while True:
                chunk = in_file.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_DOWNLOAD_BYTES:
                    raise CommandError(
                        f"Downloaded source exceeds max size of {MAX_DOWNLOAD_BYTES} bytes."
                    )
                out_file.write(chunk)
    except OSError:
        out_path.unlink(missing_ok=True)
        raise
    except CommandError:
        out_path.unlink(missing_ok=True)
        raise


class Command(BaseCommand):
    help = "Generate case overview from CIAA press release content using LLM"

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
            "--priority",
            action="store_true",
            help="Enrich only cases in the priority case list",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            dest="all_cases",
            help="Enrich all DRAFT CIAA cases (explicit, same as default)",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Enable detailed debug logging",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-process cases that already have a case overview",
        )
        parser.add_argument(
            "--llm-model",
            type=str,
            default=os.environ.get("JAWAFDEHI_OVERVIEW_MODEL", "claude-sonnet-4-6"),
            help="Model id (defaults to claude-sonnet-4-6)",
        )
        parser.add_argument(
            "--llm-base-url",
            type=str,
            default=os.environ.get("JAWAFDEHI_LLM_PROXY_URL"),
            help="Base URL for LLM API (env: JAWAFDEHI_LLM_PROXY_URL)",
        )
        parser.add_argument(
            "--llm-api-key",
            type=str,
            default=os.environ.get("JAWAFDEHI_LLM_API_KEY"),
            help="API key (env: JAWAFDEHI_LLM_API_KEY)",
        )
        parser.add_argument(
            "--llm-timeout",
            type=int,
            default=int(os.environ.get("JAWAFDEHI_LLM_TIMEOUT_SECONDS", "300")),
            help="LLM request timeout in seconds (default: 300)",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        limit = options["limit"]
        case_id = options.get("case_id")
        priority = options["priority"]
        all_cases_flag = options.get("all_cases")
        verbose = options["verbose"]
        force = options["force"]

        if priority and case_id:
            raise CommandError("--priority and --case-id are mutually exclusive")

        if verbose:
            logger.setLevel(logging.DEBUG)

        self.stdout.write(
            self.style.WARNING(
                f"{'[DRY RUN] ' if dry_run else ''}"
                "Starting case overview enrichment..."
            )
        )

        cases = self._get_eligible_cases(limit, force, case_id, priority)
        if not all_cases_flag and not priority and not case_id:
            self.stdout.write(
                self.style.NOTICE(
                    "Processing all DRAFT CIAA cases (default). "
                    "Use --all to make this explicit or --priority to filter."
                )
            )

        self.stdout.write(f"Found {len(cases)} eligible CIAA DRAFT case(s) to process")

        self._fetch_source_cache(cases)

        for idx, case in enumerate(cases, 1):
            try:
                self.stdout.write(
                    f"\n[{idx}/{len(cases)}] {case.case_id} - {case.title[:80]}..."
                )
                self._process_case(case, dry_run, options)
            except Exception as e:
                self.stats["cases_failed"] += 1
                logger.exception(f"Error processing {case.case_id}: {e}")
                self.stdout.write(self.style.ERROR(f"FAILED: {case.case_id} - {e}"))

        self._print_summary(dry_run)

    # ------------------------------------------------------------------
    # Query / source cache
    # ------------------------------------------------------------------

    def _get_eligible_cases(self, limit, force, case_id, priority=False):
        queryset = Case.objects.filter(state=CaseState.DRAFT)

        if case_id:
            queryset = queryset.filter(case_id=case_id)

        if priority:
            priority_list = load_priority_cases()
            logger.info(
                "Priority mode: loaded %d case numbers across all fiscal years",
                len(priority_list),
            )
            queryset = filter_by_priority(queryset, priority_list)

        eligible = list(queryset)

        if limit is not None:
            if limit < 0:
                raise CommandError(f"--limit must be >= 0, got {limit}")
            eligible = eligible[:limit] if limit > 0 else []

        return eligible

    def _fetch_source_cache(self, cases):
        source_ids = set()
        for case in cases:
            if not case.evidence or not isinstance(case.evidence, (list, tuple)):
                continue
            for entry in case.evidence:
                if not isinstance(entry, dict):
                    continue
                if isinstance((sid := entry.get("source_id")), str) and sid.strip():
                    source_ids.add(sid)

        self._source_lookup = {
            source.source_id: source
            for source in DocumentSource.objects.filter(
                source_id__in=source_ids, is_deleted=False
            ).prefetch_related("uploaded_files")
        }
        logger.debug(f"Cached {len(self._source_lookup)} DocumentSource records")

    # ------------------------------------------------------------------
    # Per-case processing
    # ------------------------------------------------------------------

    def _process_case(self, case, dry_run, options):
        self.stats["cases_processed"] += 1

        if not case.evidence:
            self.stats["cases_skipped"] += 1
            self.stdout.write(self.style.WARNING("  SKIPPED: No evidence"))
            return

        press_release_text = self._acquire_press_release_text(case, dry_run)
        if press_release_text is None:
            return

        self.stdout.write(
            f"  Converted press release to markdown "
            f"({len(press_release_text)} chars)"
        )

        # Phase 2+ will add LLM extraction here.
        # For Phase 1, we report successful conversion and stop.
        self.stats["cases_enriched"] += 1
        if dry_run:
            self.stdout.write(
                self.style.SUCCESS(
                    "  [DRY RUN] Would generate overview from press release content"
                )
            )
        else:
            self.stdout.write(
                self.style.NOTICE(
                    "  Conversion complete; LLM extraction not yet wired (Phase 2+)"
                )
            )

    # ------------------------------------------------------------------
    # Evidence gathering & routing
    # ------------------------------------------------------------------

    def _acquire_press_release_text(self, case, dry_run):
        source = self._select_press_release_source(case)
        if not source:
            self.stats["cases_no_content"] += 1
            self.stdout.write(
                self.style.WARNING("  SKIPPED: No press release source found")
            )
            return None

        self.stdout.write(f"  Source: {self._describe_source(source)}")

        try:
            press_release_text = self._convert_source_to_markdown(source)
        except CommandError as e:
            self.stats["cases_no_content"] += 1
            self.stdout.write(
                self.style.WARNING(
                    f"  SKIPPED: Failed to convert source to markdown: {e!s}"
                )
            )
            return None
        except (ValueError, OSError) as e:
            self.stats["cases_no_content"] += 1
            self.stdout.write(
                self.style.WARNING(
                    f"  SKIPPED: Failed to convert source to markdown: {e!s}"
                )
            )
            return None

        if not press_release_text or len(press_release_text.strip()) < 50:
            self.stats["cases_no_content"] += 1
            self.stdout.write(
                self.style.WARNING("  SKIPPED: No press release markdown content")
            )
            return None

        return press_release_text

    def _select_press_release_source(self, case: Case) -> DocumentSource | None:
        source_ids = [
            item["source_id"]
            for item in (case.evidence or [])
            if isinstance(item, dict) and isinstance(item.get("source_id"), str)
        ]
        if not source_ids:
            return None

        sources = [
            s
            for s in (self._source_lookup.get(sid) for sid in source_ids)
            if s is not None
        ]
        if not sources:
            return None

        ranked = sorted(
            ((self._score_source_for_press_release(s), s) for s in sources),
            key=lambda row: row[0],
            reverse=True,
        )
        best_score, best_source = ranked[0]
        return best_source if best_score > 0 else None

    def _describe_source(self, source: DocumentSource) -> str:
        if source.uploaded_file:
            name = source.uploaded_filename or source.uploaded_file.name
            return f"uploaded file: {name} ({source.source_id})"
        uploaded = source.uploaded_files.first()
        if uploaded and uploaded.file:
            name = uploaded.filename or uploaded.file.name
            return f"uploaded file: {name} ({source.source_id})"
        urls = self._ranked_source_urls(source)
        if urls:
            parsed = urllib.parse.urlsplit(urls[0])
            redacted = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, "", "")
            )
            return f"URL: {redacted} ({source.source_id})"
        return f"{source.source_id} (no content)"

    def _score_source_for_press_release(self, source: DocumentSource) -> int:
        upload_names = [
            file.filename or Path(file.file.name).name
            for file in source.uploaded_files.all()
        ]
        url_text = " ".join(
            url for url in (source.url or []) if isinstance(url, str) and url.strip()
        )
        corpus = " ".join(
            [
                source.title or "",
                source.description or "",
                source.uploaded_filename or "",
                url_text,
                " ".join(upload_names),
            ]
        ).lower()

        score = 0
        press_keywords = [
            "press release",
            "pressrelease",
            "press-release",
            "प्रेस विज्ञप्ति",
            "विज्ञप्ति",
        ]
        ciaa_keywords = ["ciaa", "अख्तियार"]

        if any(keyword in corpus for keyword in press_keywords):
            score += 5
        if any(keyword in corpus for keyword in ciaa_keywords):
            score += 3
        if source.source_type == SourceType.OFFICIAL_GOVERNMENT:
            score += 1
        return score

    def _ranked_source_urls(self, source: DocumentSource) -> list[str]:
        urls = [
            url.strip()
            for url in (source.url or [])
            if isinstance(url, str) and url.strip()
        ]
        if not urls:
            return []

        direct_urls = [url for url in urls if self._is_direct_document_url(url)]
        non_direct_urls = [url for url in urls if url not in direct_urls]

        direct_urls.sort(key=self._source_url_priority, reverse=True)
        return direct_urls + non_direct_urls

    def _is_direct_document_url(self, url: str) -> bool:
        parsed = urllib.parse.urlparse(url)
        path = urllib.parse.unquote(parsed.path).lower()
        return path.endswith((".pdf", ".doc", ".docx"))

    def _source_url_priority(self, url: str) -> tuple[int, int]:
        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        path = urllib.parse.unquote(parsed.path).lower()
        is_ngm_store = int(host == "ngm-store.jawafdehi.org")
        is_pdf = int(path.endswith(".pdf"))
        return (is_ngm_store, is_pdf)

    # ------------------------------------------------------------------
    # Likhit conversion (source document → markdown)
    # ------------------------------------------------------------------

    def _convert_source_to_markdown(self, source: DocumentSource) -> str:
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            raise CommandError(
                "markitdown is required for overview enrichment conversion. "
                "Install conversion dependencies (markitdown + likhit plugin)."
            ) from exc

        converter = MarkItDown(enable_plugins=True)
        with tempfile.TemporaryDirectory(prefix="overview-enrichment-") as tmp_dir:
            temp_path = self._download_source_to_path(source, Path(tmp_dir))
            if temp_path:
                logger.debug("Converting uploaded source file for %s", source.source_id)
                result = converter.convert_uri(temp_path.resolve().as_uri())
                if result.text_content and len(result.text_content.strip()) >= 50:
                    return result.text_content

            ranked_urls = self._ranked_source_urls(source)

            last_error = None
            for url in ranked_urls:
                try:
                    logger.debug(
                        "Converting source URL for %s: %s", source.source_id, url
                    )
                    temp_path = self._download_url_to_path(
                        url, source.source_id, Path(tmp_dir)
                    )
                    if not temp_path:
                        last_error = f"download failed for {url}"
                        continue
                    result = converter.convert_uri(temp_path.resolve().as_uri())
                    if result.text_content and len(result.text_content.strip()) >= 50:
                        return result.text_content
                    last_error = f"insufficient content from {url}"
                except (OSError, ValueError) as e:
                    last_error = f"{url}: {e}"
                    continue

            if not ranked_urls:
                raise CommandError(
                    f"No downloadable URLs found for source {source.source_id}"
                )

            raise CommandError(
                f"Unable to convert source {source.source_id}: {last_error}"
            )

    def _download_source_to_path(
        self, source: DocumentSource, output_dir: Path
    ) -> Path | None:
        if source.uploaded_file:
            filename = _sanitize_download_filename(
                source.uploaded_filename or source.uploaded_file.name,
                source.source_id,
            )
            out_path = _confined_output_path(output_dir, filename)
            with source.uploaded_file.open("rb") as in_file:
                _copy_stream_to_path_with_limit(in_file, out_path)
            return out_path

        uploaded = source.uploaded_files.first()
        if uploaded and uploaded.file:
            filename = _sanitize_download_filename(
                uploaded.filename or uploaded.file.name,
                source.source_id,
            )
            out_path = _confined_output_path(output_dir, filename)
            with uploaded.file.open("rb") as in_file:
                _copy_stream_to_path_with_limit(in_file, out_path)
            return out_path

        return None

    def _download_url_to_path(
        self, url: str, source_id: str, output_dir: Path
    ) -> Path | None:
        url = self._validate_url_scheme(url)
        parsed = urllib.parse.urlparse(url)
        guessed_name = _sanitize_download_filename(parsed.path, source_id)
        out_path = _confined_output_path(output_dir, guessed_name)
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
            opener = urllib.request.build_opener(_SafeRedirectHandler())
            with opener.open(request, timeout=30) as response:
                _copy_stream_to_path_with_limit(response, out_path)
            return out_path
        except OSError:
            out_path.unlink(missing_ok=True)
            return None
        except CommandError:
            out_path.unlink(missing_ok=True)
            raise

    def _validate_url_scheme(self, url: str) -> str:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            _validate_host_safety(parsed.hostname)
            return url
        raise ValueError(
            f"Invalid URL '{url}'. Only http and https URLs are allowed with a host."
        )

    # ------------------------------------------------------------------
    # Summary output
    # ------------------------------------------------------------------

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
