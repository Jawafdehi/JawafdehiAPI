"""Convert Jawafdehi source artifacts to plain Markdown using `likhit`.

`likhit` is Jawafdehi's MarkItDown plugin for Nepali PDFs / legacy docs.
We only convert sources that do not already have markdown attached.

This module is the shared conversion core: both the review pipeline
(``review.runner``) and the ``reprocess_source_markdown`` management command go
through ``convert_case_to_attach_candidates`` so the "convert a case's sources
to markdown the upstream should store" logic lives in exactly one place.
"""

import functools
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings

from . import jds_client

# A YAML frontmatter block at the very top of a document (to strip before
# re-emitting, so re-runs don't stack frontmatter).
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n+", re.DOTALL)

# Lazy singleton MarkItDown instance (loading plugins is expensive).
_md = None
_ocr_dpi_patched = False


def _patch_likhit_ocr_dpi():
    """likhit renders PDF pages for OCR at a hardcoded 300 DPI, which produces
    images (~2.7 MB) that exceed the Bedrock chat-completions payload limit
    ("length limit exceeded"). 150 DPI stays under the limit and still OCRs
    Nepali Devanagari cleanly. likhit exposes no DPI setting, so we override its
    full-page-OCR renderer with a DPI-configurable copy (LIKHIT_OCR_DPI, default
    150). Patch once, at import; leaves the installed package untouched."""
    global _ocr_dpi_patched
    if _ocr_dpi_patched:
        return
    import io

    import fitz
    from likhit.converters import nepali_pdf

    dpi = int(os.getenv("LIKHIT_OCR_DPI", "150"))

    def _run_full_page_ocr(raw, ocr_service):
        markdown_parts = []
        doc = fitz.open(stream=raw, filetype="pdf")
        try:
            for page_number in range(1, doc.page_count + 1):
                page = doc[page_number - 1]
                pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72))
                image_stream = io.BytesIO(pixmap.tobytes("png"))
                image_stream.seek(0)
                ocr_result = ocr_service.extract_text(image_stream)
                extracted_text = ocr_result.text.strip()
                formatted_text = nepali_pdf._format_full_page_ocr_text(extracted_text)
                markdown_parts.append(formatted_text if extracted_text else "")
                markdown_parts.append("")
        finally:
            doc.close()
        from markitdown import DocumentConverterResult

        return DocumentConverterResult(markdown="\n".join(markdown_parts).strip())

    nepali_pdf._run_full_page_ocr = _run_full_page_ocr
    _ocr_dpi_patched = True


def _markitdown():
    global _md
    if _md is None:
        from markitdown import MarkItDown

        _patch_likhit_ocr_dpi()
        _md = MarkItDown(enable_plugins=True)
    return _md


def _pick_url(urls):
    """Prefer a direct (non web-archive) artifact url; fall back to first."""
    if not urls:
        return None
    direct = [u for u in urls if "web.archive.org" not in u]
    return (direct or urls)[0]


def _ext_from_url(url, content_type=""):
    path = urlparse(url).path.lower()
    for ext in (".pdf", ".docx", ".doc", ".html", ".htm", ".txt"):
        if path.endswith(ext):
            return ext
    if "pdf" in content_type:
        return ".pdf"
    if "word" in content_type or "officedocument" in content_type:
        return ".docx"
    if "html" in content_type:
        return ".html"
    return ".bin"


def _with_frontmatter(body, source):
    """Prepend a YAML frontmatter block to a converted-markdown `body`.

    Frontmatter carries the processing timestamp plus source metadata so each
    stored markdown file is self-describing. The timestamp is stamped fresh on
    every emit, so it is NOT part of the on-disk cache (the cache holds only the
    deterministic body); any pre-existing frontmatter on `body` is stripped first
    so re-runs don't stack blocks.
    """
    from django.utils import timezone

    body = _FRONTMATTER_RE.sub("", body or "").lstrip("\n")
    if not body.strip():
        # No real content: stay empty so callers' `markdown.strip()` checks still
        # treat this source as "nothing to attach" (a frontmatter-only file is
        # not a usable conversion).
        return ""
    fields = {
        "processed_at": timezone.now().isoformat(),
        "source_id": source.get("source_id") or "",
        "title": source.get("title") or "",
        "source_type": source.get("source_type") or "",
        "source_url": _pick_url(source.get("url", [])) or "",
    }
    lines = ["---"]
    for key, value in fields.items():
        # json.dumps gives safe YAML-compatible scalar quoting for arbitrary
        # strings (colons, quotes, unicode) without a YAML dependency.
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n\n" + body


def convert_source(source, *, overwrite=False):
    """Return markdown text for a single source dict.

    Returns dict: {markdown, status, url, note}
    status in {attached, converted, skipped, error}

    When ``overwrite`` is True the "markdown already present on source"
    short-circuit is bypassed so the artifact is re-downloaded and re-converted
    via likhit (the url-hash cache is still honored — same bytes in, same
    markdown out). This is used by the reprocess command to refresh markdown
    after a converter change (e.g. a new likhit OCR DPI).
    """
    # 1. If markdown already attached to the source, use it (unless overwriting).
    if source.get("markdown") and not overwrite:
        return {
            "markdown": source["markdown"],
            "status": "attached",
            "url": None,
            "note": "Markdown already present on source.",
        }

    url = _pick_url(source.get("url", []))
    if not url:
        return {
            "markdown": "",
            "status": "skipped",
            "url": None,
            "note": "Source has no url to convert.",
        }

    # 2. Cache by content hash of url. The cache key is the url, NOT the
    #    converter version, so `overwrite` must bypass the cache READ as well as
    #    the "already attached" short-circuit — otherwise refreshing markdown
    #    after a converter change (e.g. a new likhit OCR DPI) would just re-serve
    #    the stale cached output. We still WRITE the fresh result below.
    # The cache holds the deterministic converted BODY — never the frontmatter,
    # whose timestamp is stamped fresh on every emit below.
    cache_key = hashlib.sha256(url.encode()).hexdigest()[:24]
    cache_path = Path(settings.SOURCE_MARKDOWN_DIR) / f"{cache_key}.md"
    if cache_path.exists() and not overwrite:
        body = cache_path.read_text(encoding="utf-8")
        return {
            "markdown": _with_frontmatter(body, source),
            "status": "converted",
            "url": url,
            "note": "From cache.",
        }

    try:
        content, ctype = jds_client.download_source_file(url)
        ext = _ext_from_url(url, ctype)
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
            tf.write(content)
            tmp = tf.name
        try:
            result = _markitdown().convert(tmp)
            md_text = result.text_content or ""
        finally:
            os.unlink(tmp)
        cache_path.write_text(md_text, encoding="utf-8")
        return {
            "markdown": _with_frontmatter(md_text, source),
            "status": "converted",
            "url": url,
            "note": f"Converted via likhit ({ext}).",
        }
    except Exception as e:  # noqa: BLE001 - surface conversion failure per source
        return {
            "markdown": "",
            "status": "error",
            "url": url,
            "note": f"Conversion failed: {e}",
        }


def convert_all(sources, *, overwrite=False):
    """Convert a list of source dicts; attach `markdown` + `conversion`.

    Each source conversion is wrapped in a hard wall-clock timeout
    (settings.CONVERT_SOURCE_TIMEOUT). A single stalled artifact (e.g. a scanned
    PDF whose Bedrock OCR never returns) is marked conversion_status=error
    instead of blocking the entire review in stage `converting_sources`.

    ``overwrite`` is forwarded to ``convert_source`` (see its docstring).
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout

    timeout = getattr(settings, "CONVERT_SOURCE_TIMEOUT", 180)
    _convert = functools.partial(convert_source, overwrite=overwrite)
    out = []
    for s in sources:
        # Run each conversion in its own short-lived executor so we can abandon
        # it on timeout. The worker thread may keep running (it's a daemon-ish
        # pool we drop the reference to), but the pipeline is no longer blocked.
        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(_convert, s)
        try:
            res = fut.result(timeout=timeout)
        except FutureTimeout:
            url = _pick_url(s.get("url", []))
            res = {
                "markdown": "",
                "status": "error",
                "url": url,
                "note": f"Conversion timed out after {timeout}s (artifact skipped).",
            }
            fut.cancel()
        except Exception as e:  # noqa: BLE001 - never let one source kill the batch
            res = {
                "markdown": "",
                "status": "error",
                "url": _pick_url(s.get("url", [])),
                "note": f"Conversion failed: {e}",
            }
        finally:
            # Don't wait on a stuck worker; let it die with the process.
            ex.shutdown(wait=False)
        s = dict(s)
        s["markdown"] = res["markdown"]
        s["conversion_status"] = res["status"]
        s["conversion_note"] = res["note"]
        out.append(s)
    return out


def _source_has_markdown_link(source):
    """True if a (converted) source dict already carries a MARKDOWN-role url."""
    if source.get("markdown_url"):
        return True
    for item in source.get("urls") or []:
        if isinstance(item, dict) and item.get("role") == "MARKDOWN":
            return True
    return False


def convert_case_to_attach_candidates(case, *, overwrite=False):
    """Convert a case's document sources and return attach candidates.

    Shared by ``review.runner`` (the graded review pipeline) and the
    ``reprocess_source_markdown`` command, so the rule for "which converted
    sources should have their markdown stored upstream" lives in one place.

    Returns ``(converted, candidates)`` where:
      - ``converted`` is the list of source dicts with ``markdown`` /
        ``conversion_status`` / ``conversion_note`` attached (the runner feeds
        this to its Bedrock analysis + scoring).
      - ``candidates`` is ``[{"source_id", "markdown"}]`` — the sources whose
        markdown should be POSTed to the upstream ``/sources/{id}/markdown/``
        endpoint.

    Default (``overwrite=False``): only sources we actually converted (status
    ``converted``) that carry a real source_id, non-empty markdown, and do NOT
    already have a MARKDOWN link. This is the long-standing poller behavior.

    With ``overwrite=True``: also include sources whose markdown was already
    attached (status ``attached``) and ignore any existing MARKDOWN link, so the
    upstream replaces it (server-side ``attach_markdown(overwrite=True)`` does
    the replace). Used to refresh markdown after a converter change.
    """
    sources = jds_client.extract_sources(case)
    converted = convert_all(sources, overwrite=overwrite)

    candidates = []
    for s in converted:
        sid = s.get("source_id")
        md = s.get("markdown") or ""
        if not (sid and md.strip()):
            continue
        if overwrite:
            # Refresh: accept freshly converted markdown (and re-emit markdown we
            # short-circuited as "attached"); the upstream replaces existing.
            if s.get("conversion_status") in ("converted", "attached"):
                candidates.append({"source_id": sid, "markdown": md})
        else:
            # Only attach when WE produced the markdown for a source that has no
            # MARKDOWN link yet.
            if s.get(
                "conversion_status"
            ) == "converted" and not _source_has_markdown_link(s):
                candidates.append({"source_id": sid, "markdown": md})

    return converted, candidates
