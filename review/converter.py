"""Convert Jawafdehi source artifacts to plain Markdown using `likhit`.

`likhit` is Jawafdehi's MarkItDown plugin for Nepali PDFs / legacy docs.
We only convert sources that do not already have markdown attached.
"""

import hashlib
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings

from . import jds_client

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


def convert_source(source):
    """Return markdown text for a single source dict.

    Returns dict: {markdown, status, url, note}
    status in {attached, converted, skipped, error}
    """
    # 1. If markdown already attached to the source, use it.
    if source.get("markdown"):
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

    # 2. Cache by content hash of url.
    cache_key = hashlib.sha256(url.encode()).hexdigest()[:24]
    cache_path = Path(settings.SOURCE_MARKDOWN_DIR) / f"{cache_key}.md"
    if cache_path.exists():
        return {
            "markdown": cache_path.read_text(encoding="utf-8"),
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
            "markdown": md_text,
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


def convert_all(sources):
    """Convert a list of source dicts; attach `markdown` + `conversion`.

    Each source conversion is wrapped in a hard wall-clock timeout
    (settings.CONVERT_SOURCE_TIMEOUT). A single stalled artifact (e.g. a scanned
    PDF whose Bedrock OCR never returns) is marked conversion_status=error
    instead of blocking the entire review in stage `converting_sources`.
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout

    timeout = getattr(settings, "CONVERT_SOURCE_TIMEOUT", 180)
    out = []
    for s in sources:
        # Run each conversion in its own short-lived executor so we can abandon
        # it on timeout. The worker thread may keep running (it's a daemon-ish
        # pool we drop the reference to), but the pipeline is no longer blocked.
        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(convert_source, s)
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
