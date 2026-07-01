"""Worker-side handler for the ``material_convert`` job kind (source → markdown).

This runs in the JOBS CONSUMER process (``review_poller --kinds material_convert``),
NOT in the API. Given the source document URL(s) resolved server-side into
``payload['source_urls']`` (by ``materials.conversion.build_convert_payload``), it
converts the document to Nepali-aware Markdown and returns the text. The server's
``on_result`` hook (``apply_convert_result``) persists it as ``data["text"]`` + a
MARKDOWN MediaObject.

Conversion reuses the **in-repo** casework converter (``review.converter``), which
wraps ``likhit`` / MarkItDown (both declared deps) — download, Devanagari OCR at a
Bedrock-safe DPI, and on-disk caching are already handled there. We deliberately do
NOT depend on any code outside this repo. The heavy deps (likhit/fitz/markitdown)
load lazily inside ``review.converter``, so importing this module stays cheap and
the API process never pulls them in.

Handler signature (the poller's contract):
    handler(payload: dict, *, on_stage: Callable[[str], None]) -> dict
"""

from __future__ import annotations

from typing import Callable


def _convert_with_timeout(convert_source, url: str) -> dict:
    """Run ``convert_source({"url":[url]})`` under a hard wall-clock timeout.

    Mirrors ``review.converter.convert_all``: a single stalled artifact (a slow
    host, or a scan whose OCR never returns) is abandoned as an ``error`` result
    rather than pinning the consumer thread until the job lease lapses. The
    timeout is ``settings.CONVERT_SOURCE_TIMEOUT`` (default 180s).
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout

    from django.conf import settings

    timeout = getattr(settings, "CONVERT_SOURCE_TIMEOUT", 180)
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(convert_source, {"url": [url]})
    try:
        return fut.result(timeout=timeout)
    except FutureTimeout:
        fut.cancel()
        return {
            "markdown": "",
            "status": "error",
            "url": url,
            "note": f"conversion timed out after {timeout}s",
        }
    finally:
        ex.shutdown(wait=False)


def handle_material_convert(
    payload: dict,
    *,
    on_stage: Callable[[str], None],
) -> dict:
    """Convert the source document(s) → ``{"text", "source_url"}``.

    Tries each ``source_urls`` entry in order (they are alternates of the same
    document — RAW, then ALTERNATE, PERMALINK); the first that yields markdown
    wins. ``on_stage`` pings progress so the job lease is extended. Raises if no
    source produced any text (a retryable failure).
    """
    # Local import: review.converter lazily loads likhit/markitdown/fitz, which
    # we want to happen only in the consumer, never at API import time.
    from review.converter import convert_source

    urls = payload.get("source_urls") or []
    if not urls:
        raise ValueError("material_convert payload has no 'source_urls'.")

    last_note = ""
    for url in urls:
        on_stage(f"convert:{url[:48]}")
        # convert_source takes a source dict ({"url": [...], "markdown"?}) and
        # returns {"markdown", "status", "url", "note"}; it downloads + OCRs +
        # caches internally. It has NO internal wall-clock timeout (only
        # review.convert_all does), so wrap it the same way review does — a
        # stalled host/OCR must not pin the consumer past the job lease.
        result = _convert_with_timeout(convert_source, url)
        text = (result.get("markdown") or "").strip()
        if text:
            on_stage(f"convert:done {len(text)}c")
            return {"text": text, "source_url": result.get("url") or url}
        last_note = result.get("note") or result.get("status") or ""
        on_stage(f"convert:empty {result.get('status', '')}")

    raise RuntimeError(
        f"material_convert: all {len(urls)} source(s) produced no text; "
        f"last: {last_note}"
    )


#: kind -> worker-side handler, merged into the poller's HANDLERS registry.
HANDLERS = {"material_convert": handle_material_convert}
