"""
Case overview enrichment service.

Phase 1: Evidence gathering, likhit conversion (DOCX/PDF → markdown),
evidence classification/routing to target sections.

See parent plan v3 for architecture details.
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

from django.core.management.base import CommandError

from cases.models import Case, DocumentSource, SourceType

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 16 * 1024
DEFAULT_LLM_TIMEOUT = 300

# ── Section ordering (क → ज) ──────────────────────────────────────────
SECTION_ORDER = [
    "short_description",
    "क",
    "ख",
    "ग",
    "घ",
    "ङ",
    "च",
    "छ",
    "ज",
]

SECTION_KEY_MAP = {
    "short_description": "short_description",
    "क": "charge_sheet_summary",
    "ख": "legal_provisions",
    "ग": "evidence_summary",
    "घ": "defense",
    "ङ": "special_court_verdict",
    "च": "appeal_summary",
    "छ": "supreme_court_verdict",
    "ज": "formal_observation",
}

# ── Evidence budget per section (chars) ───────────────────────────────
SECTION_BUDGET = {
    "short_description": 5_000,
    "क": 15_000,
    "ख": 10_000,
    "ग": 15_000,
    "घ": 10_000,
    "ङ": 15_000,
    "च": 10_000,
    "छ": 10_000,
    "ज": 5_000,
}

# ── Routing table: source_type → (sections, priority) ─────────────────
ROUTING_TABLE: dict[str, tuple[list[str], int]] = {
    SourceType.OFFICIAL_GOVERNMENT: (["क", "ख", "ग"], 10),
    SourceType.LEGAL_PROCEDURAL: (["ख"], 8),
    SourceType.FINANCIAL_FORENSIC: (["ग"], 9),
    SourceType.INTERNAL_CORPORATE: (["ग"], 7),
    SourceType.MEDIA_NEWS: (["क", "ग"], 3),
    SourceType.INVESTIGATIVE_REPORT: (["क", "ग"], 3),
    SourceType.PUBLIC_COMPLAINT: (["क"], 2),
    SourceType.LEGISLATIVE_DOC: (["ख"], 4),
    SourceType.SOCIAL_MEDIA: (["ग"], 1),
    SourceType.OTHER_VISUAL: (["ग"], 1),
}

# ── Court-stage overrides (LEGAL_COURT_ORDER → section by description) ─
COURT_STAGE_KEYWORDS: dict[str, list[str]] = {
    "ङ": [
        "special court", "विशेष अदालत", "vishesh adalat",
        "special bench", "विशेष इजलास",
    ],
    "च": [
        "high court", "उच्च अदालत", "uchcha adalat",
        "appeal", "पुनरावेदन", "appeal court",
    ],
    "छ": [
        "supreme court", "सर्वोच्च अदालत", "sarbochha adalat",
        "supreme bench", "सर्वोच्च इजलास",
    ],
    "घ": [
        "defense", "प्रतिवादी", "statement", "बयान",
        "defence", "प्रतिरक्षा",
    ],
    "ज": [
        "observation", "precedent", "नजिर",
        "नजर", "formal observation",
    ],
}

# ── Untyped source detection heuristics (filename/title) ───────────────
UNTYPED_DETECTION_RULES: list[tuple[list[str], str]] = [
    (
        ["order", "faisala", "आदेश", "फैसला", "verdict", "निर्णय"],
        SourceType.LEGAL_COURT_ORDER,
    ),
    (
        ["charge", "arrest", "investigation", "chargesheet", "अभियोग"],
        SourceType.OFFICIAL_GOVERNMENT,
    ),
    (
        ["bank", "audit", "financial", "लेखापरीक्षण"],
        SourceType.FINANCIAL_FORENSIC,
    ),
    (
        ["press release", "pressrelease", "press-release", "प्रेस विज्ञप्ति", "विज्ञप्ति"],
        SourceType.OFFICIAL_GOVERNMENT,
    ),
    (
        ["complaint", "उजुरी", "whistleblower"],
        SourceType.PUBLIC_COMPLAINT,
    ),
    (
        ["media", "news", "समाचार", "report"],
        SourceType.MEDIA_NEWS,
    ),
]

# SSRF protection
_CLOUD_METADATA_IP = "169.254.169.254"  # NOSONAR

_SSRF_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "metadata.google.internal",
    _CLOUD_METADATA_IP,
    "metadata",
    "0.0.0.0",
})


# ── SSRF / download safety ────────────────────────────────────────────

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
            "Only resolvable public hosts are allowed."
        ) from exc
    for info in addrinfo:
        addr = ipaddress.ip_address(info[4][0])
        if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved:
            raise ValueError(
                f"Blocked internal address: {hostname!r} -> {addr}. "
                "Download sources must target public IPs only."
            )


class _SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise urllib.error.HTTPError(
                req.full_url, code,
                f"Unsafe redirect scheme/host to {newurl}",
                headers, fp,
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
        raise CommandError(
            f"Refusing to write outside output directory: '{filename}'"
        )
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


# ── Evidence gathering + conversion ───────────────────────────────────

def convert_source_to_markdown(source: DocumentSource) -> str | None:
    """Convert a DocumentSource to markdown text via markitdown + likhit plugin.

    Attempts: 1) uploaded file, 2) uploads relation, 3) URL download.
    Returns None if no content could be extracted.
    """
    try:
        from markitdown import MarkItDown
    except ImportError as exc:
        raise CommandError(
            "markitdown is required for case overview enrichment. "
            "Install conversion dependencies (markitdown + likhit plugin)."
        ) from exc

    converter = MarkItDown(enable_plugins=True)

    with tempfile.TemporaryDirectory(prefix="overview-enrich-") as tmp_dir:
        tmp = Path(tmp_dir)

        # 1) Uploaded file
        temp_path = _download_source_upload(source, tmp)
        if temp_path:
            result = converter.convert_uri(temp_path.resolve().as_uri())
            if result.text_content and len(result.text_content.strip()) >= 50:
                return result.text_content

        # 2) Downloadable URLs
        for url in _ranked_source_urls(source):
            try:
                temp_path = _download_url_to_path(url, source.source_id, tmp)
                if not temp_path:
                    continue
                result = converter.convert_uri(temp_path.resolve().as_uri())
                if result.text_content and len(result.text_content.strip()) >= 50:
                    return result.text_content
            except (OSError, ValueError):
                continue

        # 3) Long description fallback
        if source.description and len(source.description.strip()) >= 500:
            return source.description

    return None


def _download_source_upload(source: DocumentSource, output_dir: Path) -> Path | None:
    """Download an uploaded file from a DocumentSource to a temp path."""
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


def _download_url_to_path(url: str, source_id: str, output_dir: Path) -> Path | None:
    """Download a URL to a temp file. Returns path or None."""
    url = _validate_url_scheme(url)
    parsed = urllib.parse.urlparse(url)
    guessed_name = _sanitize_download_filename(parsed.path, source_id)
    out_path = _confined_output_path(output_dir, guessed_name)
    try:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                ),
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


def _validate_url_scheme(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        _validate_host_safety(parsed.hostname)
        return url
    raise ValueError(
        f"Invalid URL '{url}'. Only http and https URLs are allowed."
    )


def _ranked_source_urls(source: DocumentSource) -> list[str]:
    """Return source URLs sorted by priority (direct document, then others)."""
    urls = [
        url.strip()
        for url in (source.url or [])
        if isinstance(url, str) and url.strip()
    ]
    if not urls:
        return []

    direct = [u for u in urls if _is_direct_document_url(u)]
    other = [u for u in urls if u not in direct]
    direct.sort(key=_source_url_priority, reverse=True)
    return direct + other


def _is_direct_document_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    path = urllib.parse.unquote(parsed.path).lower()
    return path.endswith((".pdf", ".doc", ".docx"))


def _source_url_priority(url: str) -> tuple[int, int, int]:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    path = urllib.parse.unquote(parsed.path).lower()
    return (int(host == "ngm-store.jawafdehi.org"),
            int(path.endswith(".pdf")),
            int(path.endswith((".pdf", ".doc", ".docx"))))


# ── Content deduplication ─────────────────────────────────────────────

def content_hash(text: str) -> str:
    """SHA-256 of normalized text for deduplication."""
    normalized = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def deduplicate_evidence(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove duplicate evidence entries by content hash."""
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        text = item.get("markdown", "")
        h = content_hash(text)
        if h in seen:
            continue
        seen.add(h)
        result.append(item)
    return result


# ── Evidence-to-section routing ───────────────────────────────────────

def detect_source_type(source: DocumentSource) -> str | None:
    """Auto-detect source_type from filename/title heuristics."""
    corpus = " ".join([
        source.title or "",
        source.description or "",
        source.uploaded_filename or "",
        *(str(u) for u in (source.url or []) if isinstance(u, str)),
    ]).lower()

    for keywords, src_type in UNTYPED_DETECTION_RULES:
        if any(kw in corpus for kw in keywords):
            return src_type

    return None


def route_evidence(
    source: DocumentSource,
    markdown_text: str,
) -> list[tuple[str, int]]:
    """Determine which sections this evidence routes to.

    Returns list of (section_key, priority) tuples.
    """
    source_type = source.source_type
    if not source_type:
        source_type = detect_source_type(source)

    if not source_type:
        return [("क", 1), ("ग", 1)]

    # LEGAL_COURT_ORDER → specialized routing by court stage
    if source_type == SourceType.LEGAL_COURT_ORDER:
        return _route_court_order(source, markdown_text)

    # Standard routing table
    sections, priority = ROUTING_TABLE.get(source_type, (["क", "ग"], 1))
    return [(s, priority) for s in sections]


def _route_court_order(
    source: DocumentSource,
    markdown_text: str,
) -> list[tuple[str, int]]:
    """Route LEGAL_COURT_ORDER evidence by court-stage keywords."""
    corpus = " ".join([
        source.title or "",
        source.description or "",
        source.uploaded_filename or "",
        markdown_text[:5000] or "",
        *(str(u) for u in (source.url or []) if isinstance(u, str)),
    ]).lower()

    routes: list[tuple[str, int]] = []

    for section_key, keywords in COURT_STAGE_KEYWORDS.items():
        if any(kw in corpus for kw in keywords):
            routes.append((section_key, 10))

    if not routes:
        # Untyped court order → all court stages + defense + observation
        routes = [(s, 6) for s in ["ङ", "च", "छ", "घ", "ज"]]

    return routes


def gather_evidence_for_case(
    case: Case,
) -> list[dict[str, Any]]:
    """Gather and convert all evidence for a case.

    Returns list of dicts with keys: source_id, source_type, markdown, sections.
    Deduplicated by content hash.
    """
    if not case.evidence or not isinstance(case.evidence, (list, tuple)):
        return []

    source_lookup = _build_source_lookup(case)
    items: list[dict[str, Any]] = []

    for entry in case.evidence:
        if not isinstance(entry, dict):
            continue
        source_id = entry.get("source_id")
        if not isinstance(source_id, str) or not source_id.strip():
            continue

        source = source_lookup.get(source_id)
        if source is None:
            logger.debug(f"Source not found: {source_id}")
            continue

        try:
            markdown = convert_source_to_markdown(source)
        except CommandError:
            logger.debug(f"Conversion failed for {source_id}", exc_info=True)
            continue

        if not markdown or len(markdown.strip()) < 50:
            logger.debug(f"No usable content from {source_id}")
            continue

        sections = route_evidence(source, markdown)
        items.append({
            "source_id": source_id,
            "source_type": source.source_type,
            "markdown": markdown,
            "sections": sections,
        })

    return deduplicate_evidence(items)


def _build_source_lookup(case: Case) -> dict[str, DocumentSource]:
    """Build a lookup map of source_id → DocumentSource for a case."""
    source_ids = set()
    for entry in (case.evidence or []):
        if isinstance(entry, dict) and isinstance(entry.get("source_id"), str):
            source_ids.add(entry["source_id"])

    sources = DocumentSource.objects.filter(
        source_id__in=source_ids,
        is_deleted=False,
    ).prefetch_related("uploaded_files")

    return {s.source_id: s for s in sources}


# ── Evidence budget trimming ──────────────────────────────────────────

def trim_evidence_for_section(
    markdown: str,
    section_key: str,
) -> str:
    """Trim evidence text to budget for the target section."""
    budget = SECTION_BUDGET.get(section_key, 5_000)
    if len(markdown) <= budget:
        return markdown
    return markdown[:budget] + "\n\n[truncated — evidence budget exceeded]"


def group_evidence_by_section(
    evidence_items: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group evidence items by target section, respecting budgets.

    Returns dict mapping section_key → list of evidence entries.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in evidence_items:
        sections = item.get("sections", [])
        if not sections:
            sections = [("क", 1)]
        for section_key, _priority in sections:
            grouped.setdefault(section_key, []).append(item)
    return grouped


# ── Main orchestrator ─────────────────────────────────────────────────

class OverviewEnricher:
    """Orchestrates the case overview enrichment pipeline.

    Phase 1: Evidence gathering + routing (this module).
    Phase 2+: LLM generation (future phases).
    """

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        dry_run: bool = False,
        verbose: bool = False,
        stdout=None,
        style=None,
    ):
        self.model = model
        self.dry_run = dry_run
        self.verbose = verbose
        self.stdout = stdout
        self.style = style or _dummy_style()

        self.stats = {
            "cases_processed": 0,
            "cases_enriched": 0,
            "cases_skipped": 0,
            "cases_no_content": 0,
            "cases_failed": 0,
            "sections_generated": {},
        }

    def process_case(self, case: Case) -> None:
        """Process a single case: gather evidence, route it, generate sections."""
        self.stats["cases_processed"] += 1

        # Step 1: Gather + convert evidence
        evidence_items = gather_evidence_for_case(case)
        if not evidence_items:
            self.stats["cases_no_content"] += 1
            self._write(self.style.WARNING(
                f"  SKIPPED: No evidence or no convertible content"
            ))
            return

        if self.verbose:
            for item in evidence_items:
                sections_str = ", ".join(
                    f"{k}" for k, _ in item["sections"]
                )
                self._write(
                    f"  Evidence: {item['source_id']} "
                    f"({len(item['markdown'])} chars) → [{sections_str}]"
                )

        # Step 2: Route evidence to sections
        evidence_by_section = group_evidence_by_section(evidence_items)

        if self.verbose:
            self._write(
                f"  Routed to {len(evidence_by_section)} section(s): "
                + ", ".join(sorted(evidence_by_section.keys()))
            )

        # Step 3 (Phase 2+): LLM generation per section
        # Stub — Phase 2 will implement LLM calls here.
        self._write(
            self.style.NOTICE(
                f"  [Phase 1] Evidence gathered and routed. "
                f"LLM generation (Phase 2+) not yet implemented — "
                f"evidence routed to sections: "
                f"{', '.join(sorted(evidence_by_section.keys()))}"
            )
        )

        if self.dry_run:
            self.stats["cases_enriched"] += 1
            self._write(
                self.style.SUCCESS(
                    f"  [DRY RUN] Would generate sections for "
                    f"{len(evidence_by_section)} section(s)"
                )
            )
        else:
            self.stats["cases_enriched"] += 1
            self._write(
                self.style.SUCCESS(
                    f"  [Phase 1] Evidence gathered for {len(evidence_items)} source(s)"
                )
            )

    def _write(self, msg: str) -> None:
        if self.stdout:
            self.stdout.write(msg)


class _dummy_style:
    @staticmethod
    def SUCCESS(msg): return msg
    WARNING = SUCCESS
    ERROR = SUCCESS
    NOTICE = SUCCESS


def build_stats_summary(stats, stdout, style) -> None:
    """Print per-section statistics."""
    if stats.get("sections_generated"):
        stdout.write("Sections generated:")
        for key in SECTION_ORDER:
            count = stats["sections_generated"].get(key, 0)
            if count:
                stdout.write(f"  {key}: {count}")
