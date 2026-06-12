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
    """Prefer a direct (non web-archive) artifact url; fall back to first.

    Operates on a plain list of url STRINGS (the legacy ``source['url']``
    shape). Role-aware selection lives in ``_pick_source_link``.
    """
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


def _ext_from_magic(content):
    """Detect a file's true extension from its leading magic bytes, or None.

    Source files are routed to a converter by URL extension, but uploads are
    frequently MISLABELED — most commonly a modern ``.docx`` (a ZIP/OOXML
    container) saved with a ``.doc`` name. antiword then rejects it ("not a Word
    Document ... seems to be a ZIP"). Sniffing the real container lets us route
    such files to the right converter (MarkItDown handles ``.docx`` cleanly).

    Only disambiguates the Office family we actually confuse:
      - ``PK\\x03\\x04`` (ZIP)  -> ``.docx``  (OOXML)
      - ``\\xd0\\xcf\\x11\\xe0`` (OLE2) -> ``.doc``  (legacy binary Word)
    Returns None for anything else (PDF/HTML/txt/etc. keep their URL ext).
    """
    if not content:
        return None
    head = content[:8]
    if head[:4] == b"PK\x03\x04":
        return ".docx"
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return ".doc"
    return None


def _find_libreoffice():
    """Return the LibreOffice/soffice executable path, or None if not installed.

    Honors settings.LIBREOFFICE_BINARY, then searches PATH for the common names
    (the Document Foundation RPMs install a versioned `libreoffice26.2`).
    """
    import shutil

    explicit = getattr(settings, "LIBREOFFICE_BINARY", "") or ""
    if explicit:
        return explicit if os.path.exists(explicit) else None
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    # Versioned binaries (e.g. libreoffice26.2) installed under /usr/bin.
    import glob

    for path in sorted(glob.glob("/usr/bin/libreoffice*")):
        if os.access(path, os.X_OK):
            return path
    return None


def _libreoffice_to_docx(content, ext, soffice):
    """Convert a legacy Word document (bytes) to .docx with LibreOffice headless.

    Returns the .docx bytes, or None on failure. Each call uses a private,
    short-lived LibreOffice user profile so concurrent conversions don't collide
    on the shared default profile.
    """
    import subprocess

    with tempfile.TemporaryDirectory() as work:
        src_path = os.path.join(work, f"input{ext}")
        with open(src_path, "wb") as f:
            f.write(content)
        profile = os.path.join(work, "profile")
        timeout = getattr(settings, "CONVERT_SOURCE_TIMEOUT", 180)
        try:
            subprocess.run(
                [
                    soffice,
                    "--headless",
                    f"-env:UserInstallation=file://{profile}",
                    "--convert-to",
                    "docx",
                    "--outdir",
                    work,
                    src_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=True,
            )
        except Exception:  # noqa: BLE001 - any failure -> caller falls back
            return None
        # LibreOffice names the output from the input stem (input.doc -> input.docx),
        # but glob rather than hardcode the name so an unexpected stem still works.
        import glob

        produced = glob.glob(os.path.join(work, "*.docx"))
        if not produced:
            return None
        with open(produced[0], "rb") as f:
            return f.read()


# How cleanly a format converts to markdown — higher is better. Word docs are
# structured text (best), HTML is extractable main-content, PDF may need OCR,
# everything else is a guess. Used to decide whether an ALTERNATE link is a
# better conversion target than the RAW one.
_FORMAT_RANK = {
    ".docx": 4,
    ".doc": 4,
    ".html": 3,
    ".htm": 3,
    ".pdf": 2,
    ".txt": 2,
}


def _format_rank(url):
    return _FORMAT_RANK.get(_ext_from_url(url), 1)


# Some source links were persisted with the INTERNAL Cloudflare R2 S3-API
# endpoint (``<account>.r2.cloudflarestorage.com/jawafdehi/case_uploads/...``)
# instead of the PUBLIC custom-domain form (``s3.jawafdehi.org/case_uploads/...``).
# The internal endpoint returns HTTP 400 to anonymous GETs, so the file can't be
# downloaded for conversion. Rewrite it to the public host (dropping the
# ``jawafdehi`` bucket path segment, which the custom domain maps implicitly).
_R2_INTERNAL_RE = re.compile(
    r"^https?://[0-9a-f]+\.r2\.cloudflarestorage\.com/jawafdehi/(?P<path>.+)$"
)
_PUBLIC_MEDIA_HOST = "https://s3.jawafdehi.org/"


def _normalize_media_url(url):
    """Rewrite an internal R2 S3-endpoint url to the public custom-domain url.

    Leaves every other url untouched. See ``_R2_INTERNAL_RE`` above.
    """
    if not url:
        return url
    m = _R2_INTERNAL_RE.match(url)
    if m:
        return _PUBLIC_MEDIA_HOST + m.group("path")
    return url


def _pick_source_link(source):
    """Choose which link to convert, honoring the source's link ROLES.

    Returns ``(url, role)`` or ``(None, None)``. Selection priority:

      1. RAW — the canonical document file — is the default target.
      2. ALTERNATE — an alternate-format rendering of the same document — is
         preferred over RAW only when it converts more cleanly (e.g. a ``.docx``
         export when RAW is a scanned ``.pdf``); see ``_FORMAT_RANK``.
      3. SOURCE_PAGE — the HTML page a document was published on — is a last
         resort (no document file present); it is fetched and run through
         main-content extraction to drop site chrome.

    MARKDOWN (our own output) and PERMALINK (a stable pointer, not necessarily
    fetchable) are never conversion targets. Falls back to the legacy plain
    ``url`` string list when no role-tagged ``urls`` exist.
    """
    by_role = {}
    for item in source.get("urls") or []:
        if not isinstance(item, dict):
            continue
        link, role = item.get("link"), item.get("role")
        # Normalize internal R2 endpoint urls to the public host so the picked
        # link is actually fetchable (see _normalize_media_url).
        link = _normalize_media_url(link)
        if link and role and role not in by_role:
            by_role[role] = link  # first link of each role wins

    raw = by_role.get("RAW")
    alt = by_role.get("ALTERNATE")
    if raw or alt:
        if raw and alt:
            chosen = alt if _format_rank(alt) > _format_rank(raw) else raw
        else:
            chosen = raw or alt
        return chosen, ("ALTERNATE" if chosen == alt and chosen != raw else "RAW")

    page = by_role.get("SOURCE_PAGE")
    if page:
        return page, "SOURCE_PAGE"

    # Legacy sources with only plain url strings (no roles): treat as RAW.
    legacy = _normalize_media_url(_pick_url(source.get("url", [])))
    return (legacy, "RAW") if legacy else (None, None)


def _html_to_markdown(raw_bytes):
    """Extract the MAIN content of an HTML page as Markdown (drops site chrome).

    HTML sources (SOURCE_PAGE, or a RAW/ALTERNATE link that is itself a web
    page) otherwise convert to markdown that includes nav/header/footer/sidebar
    boilerplate. trafilatura isolates the article body using text-density
    heuristics. Returns the extracted markdown, or "" when trafilatura finds no
    main content (caller then falls back to the plain MarkItDown conversion).
    """
    import trafilatura

    html = raw_bytes.decode("utf-8", errors="replace")
    extracted = trafilatura.extract(
        html,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    return (extracted or "").strip()


def _with_frontmatter(body, source, *, source_url=None):
    """Prepend a YAML frontmatter block to a converted-markdown `body`.

    Frontmatter carries the processing timestamp plus source metadata so each
    stored markdown file is self-describing. The timestamp is stamped fresh on
    every emit, so it is NOT part of the on-disk cache (the cache holds only the
    deterministic body); any pre-existing frontmatter on `body` is stripped first
    so re-runs don't stack blocks. ``source_url`` is the link actually converted
    (defaults to the role-aware pick).
    """
    from django.utils import timezone

    body = _FRONTMATTER_RE.sub("", body or "").lstrip("\n")
    if not body.strip():
        # No real content: stay empty so callers' `markdown.strip()` checks still
        # treat this source as "nothing to attach" (a frontmatter-only file is
        # not a usable conversion).
        return ""
    if source_url is None:
        source_url, _ = _pick_source_link(source)
    fields = {
        "processed_at": timezone.now().isoformat(),
        "source_id": source.get("source_id") or "",
        "title": source.get("title") or "",
        "source_type": source.get("source_type") or "",
        "source_url": source_url or "",
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

    # Choose which link to convert, honoring link roles (RAW/ALTERNATE > the
    # HTML SOURCE_PAGE; never MARKDOWN/PERMALINK).
    url, role = _pick_source_link(source)
    if not url:
        return {
            "markdown": "",
            "status": "skipped",
            "url": None,
            "note": "Source has no convertible url (RAW/ALTERNATE/SOURCE_PAGE).",
        }

    # 2. Cache by content hash of url. The cache key is the url, NOT the
    #    converter or its configuration, so `overwrite` must bypass the cache
    #    READ (as well as the "already attached" short-circuit). Otherwise any
    #    CONVERTER CHANGE re-serves stale cached output — this includes a new
    #    likhit OCR DPI AND toggling LIBREOFFICE_DOC_CONVERSION (a .doc cached via
    #    antiword would keep returning the antiword body even after LibreOffice is
    #    enabled). Re-run with --overwrite after any such change. We still WRITE
    #    the fresh result below.
    # The cache holds the deterministic converted BODY — never the frontmatter,
    # whose timestamp is stamped fresh on every emit below.
    cache_key = hashlib.sha256(url.encode()).hexdigest()[:24]
    cache_path = Path(settings.SOURCE_MARKDOWN_DIR) / f"{cache_key}.md"
    if cache_path.exists() and not overwrite:
        body = cache_path.read_text(encoding="utf-8")
        return {
            "markdown": _with_frontmatter(body, source, source_url=url),
            "status": "converted",
            "url": url,
            "note": "From cache.",
        }

    try:
        content, ctype = jds_client.download_source_file(url)
        ext = _ext_from_url(url, ctype)
        # Correct mislabeled Office files (e.g. a .docx saved with a .doc name)
        # by sniffing the real container, so the file is routed to the converter
        # that can actually read it.
        true_ext = _ext_from_magic(content)
        if true_ext and true_ext != ext:
            ext = true_ext
        # HTML sources (the SOURCE_PAGE landing page, or a RAW/ALTERNATE link
        # that is itself a web page): extract just the main content so the
        # markdown isn't polluted with nav/header/footer chrome. Fall back to the
        # plain MarkItDown conversion when extraction finds no article body.
        md_text = ""
        note = ""
        convert_ext = ext  # extension markitdown sees (LibreOffice may change it)
        if role == "SOURCE_PAGE" or ext in (".html", ".htm"):
            md_text = _html_to_markdown(content)
            if md_text:
                note = "Converted via trafilatura (HTML main-content)."
        # Legacy Word (.doc only): optionally use LibreOffice to produce a clean
        # .docx (it handles files the bundled antiword crashes on or rejects).
        # Modern .docx is left to markitdown directly — LibreOffice would be a
        # redundant round-trip. Off by default; falls back to the likhit path when
        # LibreOffice is disabled, absent, or the conversion fails.
        if not md_text and ext == ".doc":
            if getattr(settings, "LIBREOFFICE_DOC_CONVERSION", False):
                soffice = _find_libreoffice()
                if soffice:
                    docx = _libreoffice_to_docx(content, ext, soffice)
                    if docx:
                        content = docx
                        convert_ext = ".docx"
                        note = f"Converted via LibreOffice + markitdown ({ext})."
        if not md_text:
            with tempfile.NamedTemporaryFile(suffix=convert_ext, delete=False) as tf:
                tf.write(content)
                tmp = tf.name
            try:
                result = _markitdown().convert(tmp)
                md_text = result.text_content or ""
            finally:
                os.unlink(tmp)
            note = note or f"Converted via likhit ({ext})."
        cache_path.write_text(md_text, encoding="utf-8")
        return {
            "markdown": _with_frontmatter(md_text, source, source_url=url),
            "status": "converted",
            "url": url,
            "note": note,
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
            url, _ = _pick_source_link(s)
            res = {
                "markdown": "",
                "status": "error",
                "url": url,
                "note": f"Conversion timed out after {timeout}s (artifact skipped).",
            }
            fut.cancel()
        except Exception as e:  # noqa: BLE001 - never let one source kill the batch
            url, _ = _pick_source_link(s)
            res = {
                "markdown": "",
                "status": "error",
                "url": url,
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
