"""Server-side plumbing for the ``material_extract`` job kind.

``materials.conversion`` is the sibling to read first: it does this for
``material_convert``, which turns a document into one flat markdown blob for
full-text search. This one produces *structure* — the pages, tables and figures
``…/content`` serves — and the difference in what they return is why they are
two kinds rather than one. A convert result is a string the API can store in a
JSONB field; an extraction is thousands of rows and, for a 446-page report, more
than a megabyte of transcript. That cannot travel back through
``POST /api/jobs/<pk>/result/``: Django's ``DATA_UPLOAD_MAX_MEMORY_SIZE`` is
2.5 MB, so the largest documents in the archive — exactly the ones worth
extracting — would 413 at the point of handing back the work.

So ``material_extract`` is run by an **in-process** consumer with ngm database
access (``manage.py extract_worker``), the way ``court_scrape`` is, and the job
result carries counts rather than content. The queue still owns the hard parts:
atomic claims, leases with a reaper, retry with backoff, dedup, and the
``/api/jobs`` dashboard.

Three seams here:

- :func:`enqueue_material_extract` — put one document in the queue. Deduped on
  the IRI, so a reader asking twice and a batch run asking at the same time
  produce one job.
- :func:`build_extract_payload` — resolve the source document URL(s) at claim
  time, so the payload a worker runs on is the material as it is NOW rather than
  as it was when something queued the job.
- :func:`extract_and_store` — fetch, extract, write. The one place the batch
  path and the worker path meet, so they cannot drift.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings

from . import conversion

logger = logging.getLogger("materials.extract")

EXTRACT_KIND = "material_extract"

#: Only PDFs, for now. The text tier reads a PDF's own page structure
#: (``materials.extraction.pdf``), and there is no page in a .docx or an HTML
#: scrape to anchor a citation to — a "page 212" we invented for one of those
#: would be checkable by nobody. A material carrying only those is reported as
#: not extractable rather than extracted into something weaker.
_PDF_CONTENT_TYPES = ("application/pdf", "application/x-pdf")

#: Cap on a fetched document, **measured against the archive rather than guessed
#: from the CIAA corpus**. This was 64 MiB on the reasoning that the largest CIAA
#: annual report is 20 MB; surveying the Auditor General's 18 annual reports then
#: found two above that line — `oag-13463` (the 63rd, FY2083) at **79.7 MiB** and
#: `oag-11101` at 64.1 MiB — so the cap would have refused the newest and largest
#: report on the shelf, as a fetch failure that reads like a network problem.
#:
#: 128 MiB, because a cap still has to exist: this runs over 346k documents in a
#: pod with a 2 GiB limit, and one mis-linked multi-gigabyte file must fail that
#: one job loudly rather than OOM-killing a worker mid-drain. For scale, that
#: 79.7 MiB / 1,113-page report extracts at a 923 MiB peak, so the document is
#: never the largest thing in the process — note though that the fetch below
#: holds the body twice for an instant when it joins its chunks.
MAX_DOCUMENT_BYTES = getattr(settings, "DOCUMENT_EXTRACT_MAX_BYTES", 128 * 1024 * 1024)

#: Per-document fetch timeout, seconds.
FETCH_TIMEOUT = getattr(settings, "DOCUMENT_EXTRACT_FETCH_TIMEOUT", 120)

#: How many extractions may be waiting before the on-demand route stops
#: accepting more. Extraction is minutes of CPU per document, so an unbounded
#: request-triggered queue is a way to spend the whole worker budget on one
#: caller's loop over the archive. A batch run bypasses this deliberately: it is
#: an operator who meant it, running with a ``--limit`` they chose.
MAX_QUEUED_ON_DEMAND = getattr(settings, "DOCUMENT_EXTRACT_MAX_QUEUED", 500)


class DocumentFetchError(RuntimeError):
    """The source document could not be fetched — network, HTTP, or too large."""


class NothingToExtract(ValueError):
    """The job cannot run and re-running it will not change that.

    Separated from a fetch failure so the worker can tell the queue which it is:
    a mis-linked or unreadable document is not worth three attempts and a
    dead-letter entry, where a timed-out download is.
    """


def document_urls(data: dict[str, Any]) -> list[str]:
    """Ordered source-document URLs for this material, best first.

    Built on ``conversion.source_urls`` — same roles, same preference order, same
    exclusion of our own MARKDOWN output — then narrowed to what this pipeline
    can actually open. Sharing the role logic matters: a material whose RAW link
    is the document OCR reads is the same link extraction must read, and two
    independent notions of "the source document" would eventually disagree about
    which file a page citation refers to.
    """
    return [url for url in conversion.source_urls(data) if _looks_like_pdf(url, data)]


def _looks_like_pdf(url: str, data: dict[str, Any]) -> bool:
    if url.split("?")[0].lower().endswith(".pdf"):
        return True
    # R2 keys are hashed, so a stored PDF frequently has no extension in its URL
    # at all; the MediaObject's own encodingFormat is then the only statement
    # about what the bytes are.
    for media in conversion._media_list(data):
        if media.get("contentUrl") != url:
            continue
        fmt = (media.get("encodingFormat") or "").split(";")[0].strip().lower()
        if fmt in _PDF_CONTENT_TYPES:
            return True
    return False


def is_extractable(data: dict[str, Any]) -> bool:
    """Whether this material carries a document this pipeline can open."""
    return bool(document_urls(data))


def queued_count() -> int:
    """Extractions queued or running right now."""
    from jobs.models import Job

    return Job.objects.filter(
        kind=EXTRACT_KIND, status__in=Job.ACTIVE_STATUSES
    ).count()


def enqueue_material_extract(material_iri: str, *, priority: int = 100, force: bool = False):
    """Queue an extraction for ``material_iri``; returns the ``Job``.

    Deduped on the IRI, so a reader asking while a batch run already queued the
    same document joins that job rather than racing it — the two would otherwise
    both write the same document, and the second would delete the first's rows
    mid-read.

    ``force`` re-extracts a document we already hold content for. It rides in the
    payload rather than changing the dedup key: two forced runs are still one
    job, which is what we want, and the writer replaces content wholesale, so a
    forced re-run of an unchanged document is a no-op with a new timestamp.
    """
    from jobs import queue as jobs_queue

    return jobs_queue.enqueue(
        EXTRACT_KIND,
        payload={"material_iri": material_iri, "force": bool(force)},
        dedup_key=f"{EXTRACT_KIND}:{material_iri}",
        priority=priority,
    )


def build_extract_payload(job) -> dict:
    """``build_payload`` hook: resolve the document URLs for a claimed job.

    Raises when the material is gone or carries nothing extractable, so the job
    fails fast with a reason instead of a worker no-op-ing and reporting success
    over a document it never opened.
    """
    from .models import Material

    iri = (job.payload or {}).get("material_iri")
    if not iri:
        raise ValueError("material_extract payload is missing 'material_iri'.")
    row = Material.objects.filter(pk=iri, is_deleted=False).first()
    if row is None:
        raise ValueError(f"material_extract: no live Material at {iri!r}.")
    urls = document_urls(row.data or {})
    if not urls:
        raise ValueError(f"material_extract: {iri} carries no extractable document.")
    return {"source_urls": urls}


def fetch_document(url: str, *, max_bytes: int = MAX_DOCUMENT_BYTES) -> bytes:
    """Download a source document, refusing one larger than ``max_bytes``.

    Streamed and counted as it arrives rather than fetched and then measured:
    ``Content-Length`` is advisory and frequently absent on an object store
    redirect, so a check against the header alone is a check a 2 GiB file passes.
    """
    import requests

    from review.jds_client import UA

    try:
        response = requests.get(
            url, headers={"User-Agent": UA}, timeout=FETCH_TIMEOUT, stream=True
        )
    except requests.RequestException as exc:
        raise DocumentFetchError(f"could not fetch {url}: {exc}") from exc
    with response:
        if response.status_code != 200:
            raise DocumentFetchError(f"{url} returned HTTP {response.status_code}")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=262144):
            total += len(chunk)
            if total > max_bytes:
                raise DocumentFetchError(
                    f"{url} is larger than the {max_bytes // (1024 * 1024)} MiB limit"
                )
            chunks.append(chunk)
    return b"".join(chunks)


def fetch_and_extract(iri: str, urls: list[str], *, doc_id: str = "", on_stage=None):
    """``(source_url, bytes, bundle)`` for the first URL that yields a document.

    The URLs are alternates of the same document (RAW, then ALTERNATE, PERMALINK)
    and the first readable one wins. Shared by the worker and the batch command's
    ``--inline`` mode, which both need this and only one of which needs the bytes
    afterwards (to render chart pages).

    **Which failure is raised decides how many times we pay for it.** Every URL
    failing to *download* is :class:`DocumentFetchError` — an object store 503 or
    a timeout is worth another attempt. Downloading fine and not being a readable
    PDF is :class:`NothingToExtract`, which the worker reports as non-retryable:
    a material whose RAW link is an HTML error page will not become a PDF on the
    third try, and three attempts is three times the cost for the same answer.

    Never returns empty: a document nobody could open must not be stored as a
    document with no content, which a client cannot tell from one that is empty.

    The heavy imports (fitz, likhit) are function-local on purpose. This module
    is imported by the API process for :func:`enqueue_material_extract`, and that
    process must not pay for — or hold the memory of — a PDF toolchain it never
    runs.
    """
    from .extraction import ExtractionUnavailable, extract_pdf

    def stage(note: str) -> None:
        if on_stage is not None:
            on_stage(note)

    fetch_problems: list[str] = []
    read_problems: list[str] = []
    for url in urls:
        stage(f"fetch:{url[-40:]}")
        try:
            data = fetch_document(url)
        except DocumentFetchError as exc:
            fetch_problems.append(str(exc))
            continue
        stage(f"extract:{len(data) // 1024}KiB")
        try:
            bundle = extract_pdf(
                data, doc_id=doc_id or _doc_id_for(iri), source_url=url
            )
        except ExtractionUnavailable as exc:
            read_problems.append(f"{url}: {exc}")
            continue
        return url, data, bundle

    if read_problems:
        raise NothingToExtract(
            f"{iri} has no readable document: " + "; ".join(read_problems)
        )
    raise DocumentFetchError(
        f"could not fetch any source for {iri}: " + "; ".join(fetch_problems)
    )


def extraction_result(iri: str, source_url: str, bundle, written: dict) -> dict:
    """What a finished extraction reports — counts and provenance, no content.

    Deliberately small: this becomes ``job.result``, and a 446-page report's
    pages are megabytes of transcript that the jobs API's 2.5 MB body limit would
    reject outright.
    """
    return {
        "material_iri": iri,
        "source_url": source_url,
        "dataset_revision": bundle.dataset_revision,
        "page_count": bundle.page_count,
        "transcript_verdict": bundle.transcript_verdict,
        "figures_verdict": bundle.figures_verdict,
        # Page numbers only — the chart worklist, not content. Kept on the job
        # rather than in the document's rows because a chart listed with no
        # points would read as "we read this chart and it had no data".
        "chart_pages": bundle.chart_pages,
        # What the redactor removed, per class. Counts name nothing, so they are
        # safe here; the journal that names values is dropped at the source.
        "redactions": bundle.redactions,
        **written,
    }


def extract_and_store(
    iri: str, *, urls: list[str], doc_id: str = "", on_stage=None
) -> dict:
    """Fetch, extract and store one document. Returns a result summary."""
    from .document_content import store_document_content

    url, _data, bundle = fetch_and_extract(
        iri, urls, doc_id=doc_id, on_stage=on_stage
    )
    if on_stage is not None:
        on_stage("store")
    return extraction_result(iri, url, bundle, store_document_content(iri, bundle))


def handle_material_extract(payload: dict, *, on_stage=None) -> dict:
    """Run one claimed ``material_extract`` job. The worker's entry point.

    ``payload`` is the enqueued payload merged with what
    :func:`build_extract_payload` resolved at claim time, so ``source_urls`` is
    the material's links as of the claim, not as of the enqueue.

    Skips a document we already hold content for unless the payload says
    ``force``. The check is here rather than at enqueue time because the queue is
    not a to-do list: between a reader asking and a worker claiming, a batch run
    may have extracted the same document, and re-extracting it would delete and
    rewrite every row — invalidating the ETag of a document nobody changed.
    """
    from .models import DocumentContent

    iri = (payload or {}).get("material_iri")
    if not iri:
        raise NothingToExtract("material_extract payload is missing 'material_iri'.")
    urls = (payload or {}).get("source_urls") or []
    if not urls:
        raise NothingToExtract(f"material_extract: {iri} carries no document to read.")

    if not (payload or {}).get("force"):
        existing = (
            DocumentContent.objects.using("ngm")
            .filter(pk=iri)
            .values_list("dataset_revision", flat=True)
            .first()
        )
        if existing is not None:
            logger.info("material_extract %s: content already stored, skipping", iri)
            return {
                "material_iri": iri,
                "skipped": "already extracted",
                "dataset_revision": existing,
            }

    return extract_and_store(iri, urls=urls, on_stage=on_stage)


def _doc_id_for(iri: str) -> str:
    """The document's own id, for uids: the material's ident.

    The material IRI is already the primary key of the content row, so the ident
    alone identifies the document, and it is what appears in the public URL a
    reader is looking at.
    """
    return iri.rstrip("/").rsplit("/", 1)[-1]
