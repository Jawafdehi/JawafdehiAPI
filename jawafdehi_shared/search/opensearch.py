"""Shared OpenSearch client helpers for the platform's bilingual entity/document
search (the PR-91 substrate, reused by NES entity search and NGM text search).

This is a thin, framework-agnostic wrapper so each Django service configures one
client from its settings rather than duplicating connection/index code. The
heavy document-builder + EN<->Devanagari transliteration logic lives with NES
(it owns the entity index); this module standardizes connection + index naming.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("jawafdehi.search.opensearch")


def _env_int(name: str, default: int, minimum: int) -> int:
    """An int from env, clamped to ``minimum`` and falling back on garbage.

    Every knob below is documented as something an operator retunes per reindex
    job, which means the values arrive from a shell rather than from code review.
    Two shapes have to be survivable:

    * **unparseable** (``OPENSEARCH_BULK_CHUNK_SIZE=oops``) — a bare ``ValueError``
      raised from inside the bulk loop would abort a reindex half-way through
      with a traceback that names neither the variable nor the job;
    * **out of range** (``OPENSEARCH_BULK_MAX_RETRIES=-1``) — this one is worse
      than an error, because opensearch-py's ``for attempt in range(max_retries
      + 1)`` silently becomes ``range(0)``: no chunk is ever sent, nothing is
      yielded, and the reindex reports success having indexed nothing.

    So clamp rather than trust, and say so in the log instead of failing late.
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%d is below the minimum %d; clamping", name, value, minimum)
        return minimum
    return value


def get_opensearch_url() -> str:
    """Resolve the shared OpenSearch endpoint from env (OPENSEARCH_URL)."""
    return os.getenv("OPENSEARCH_URL", "http://localhost:9200")


def get_opensearch_timeout() -> float:
    """Per-request timeout (seconds) for the client, from env (OPENSEARCH_TIMEOUT).

    Defaults to 30s — well above opensearch-py's own 10s default, which is too
    short for bulk indexing of large OCR'd material docs: a full ``reindex_all``
    of ngm-materials otherwise trips the 10s read-timeout on a batch of oversized
    documents and fails deterministically part-way through. Bulk/reindex jobs can
    raise this further (e.g. ``OPENSEARCH_TIMEOUT=120``) without changing the
    serving-path default.
    """
    return float(os.getenv("OPENSEARCH_TIMEOUT", "30"))


def get_bulk_max_chunk_bytes() -> int:
    """Max serialized bytes per bulk request (OPENSEARCH_BULK_MAX_CHUNK_BYTES).

    10 MiB, against opensearch-py's 100 MB default. This is the bound that
    matters: it is the only one expressed in the units the client and the
    cluster both pay in, so it holds however large individual documents grow.
    The document-count bound cannot make that guarantee — ngm-materials docs
    average ~20 KB but carry a whole JSON-LD body in ``raw``, so a chunk's cost
    is set by its bytes, not its length.
    """
    return _env_int("OPENSEARCH_BULK_MAX_CHUNK_BYTES", 10 * 1024 * 1024, 1024)


def get_bulk_chunk_size() -> int:
    """Docs per bulk request (OPENSEARCH_BULK_CHUNK_SIZE).

    Left at opensearch-py's own 500 ON PURPOSE. Lowering it does not buy a memory
    guarantee — ``get_bulk_max_chunk_bytes`` already provides that — and it costs
    request count: this is the *count* half of a ``min(count, bytes)`` bound, so
    dropping it to 200 under a caller that hands over 500 docs splits every batch
    into 200/200/100 and triples the round-trips against a cluster that is, by
    hypothesis, already struggling to keep up.
    """
    return _env_int("OPENSEARCH_BULK_CHUNK_SIZE", 500, 1)


def get_bulk_max_retries() -> int:
    """Per-chunk retries on a 429 rejection (OPENSEARCH_BULK_MAX_RETRIES).

    opensearch-py defaults this to 0, so a single ``429 Too Many Requests`` from
    a busy cluster fails a whole reindex. Retrying with backoff is the correct
    response to backpressure. Distinct from the CLIENT-level ``max_retries`` in
    ``make_client``, which covers transport errors (timeouts, connection resets)
    rather than per-document rejections inside an otherwise-successful bulk
    response — the two are different failure modes and are tuned separately.
    """
    return _env_int("OPENSEARCH_BULK_MAX_RETRIES", 3, 0)


def get_bulk_initial_backoff() -> int:
    """Seconds before the first 429 retry (OPENSEARCH_BULK_INITIAL_BACKOFF)."""
    return _env_int("OPENSEARCH_BULK_INITIAL_BACKOFF", 2, 0)


def get_bulk_max_backoff() -> int:
    """Ceiling on a single 429 backoff sleep (OPENSEARCH_BULK_MAX_BACKOFF).

    60s, against opensearch-py's 600. The sleep is ``time.sleep`` — it blocks the
    whole reindex — and these run as CronJobs with an ``activeDeadlineSeconds``
    in the low thousands. One 600s sleep would burn a sixth of the materials
    job's budget doing nothing, and several would blow it outright, turning
    backpressure into a deadline kill that looks like a hang. Tunable because the
    right ceiling depends on the job's deadline, which this module cannot see.
    """
    return _env_int("OPENSEARCH_BULK_MAX_BACKOFF", 60, 1)


def make_client(url: str | None = None):
    """Construct an opensearch-py client. Imported lazily so the package is
    importable without the optional dependency installed (e.g. in a service that
    doesn't use search).

    Auth from env: if ``OPENSEARCH_USER`` / ``OPENSEARCH_PASSWORD`` are set
    (self-hosted with the security plugin enabled), HTTP basic auth is used and
    TLS verification is left on. With no creds (dev compose, security disabled)
    the client connects anonymously.

    Timeout/retries: the per-request timeout is set explicitly (see
    ``get_opensearch_timeout``) rather than left at opensearch-py's 10s default,
    with a small bounded retry on timeout. ``retry_on_timeout`` is safe for our
    bulk indexers because every unified-search doc is keyed by a deterministic
    IRI ``_id`` (see ``indexing.stream_bulk``), so a re-sent bulk is idempotent.
    """
    from opensearchpy import OpenSearch  # lazy: optional dependency

    user = os.getenv("OPENSEARCH_USER")
    password = os.getenv("OPENSEARCH_PASSWORD")
    kwargs: dict[str, Any] = {
        "hosts": [url or get_opensearch_url()],
        "timeout": get_opensearch_timeout(),
        "max_retries": _env_int("OPENSEARCH_MAX_RETRIES", 3, 0),
        "retry_on_timeout": True,
    }
    if user and password:
        kwargs["http_auth"] = (user, password)
    return OpenSearch(**kwargs)


# Canonical index names (one per modality), referenced by all services and the
# unified-search indexers. One index per modality (mappings/lifecycle differ).
ENTITY_INDEX = "nes-entities"  # entities -> StoredEntity
MATERIAL_INDEX = "ngm-materials"  # materials -> Material
COURTCASE_INDEX = "ngm-courtcases"  # courts -> CourtCase
CASE_INDEX = "jawafdehi-cases"  # cases -> Case (published only)

# Every index shares the bilingual analyzers + common document mapping.
ALL_INDICES: tuple[str, ...] = (
    ENTITY_INDEX,
    MATERIAL_INDEX,
    COURTCASE_INDEX,
    CASE_INDEX,
)


def create_index(
    client,
    name: str,
    settings: dict[str, Any] | None = None,
    mappings: dict[str, Any] | None = None,
) -> bool:
    """Create index ``name`` with the given ``settings``/``mappings`` if absent.

    Defaults to the shared bilingual ``index_settings()`` / ``common_mappings()``.
    Returns True if the index was created, False if it already existed (idempotent).
    """
    # Local import avoids a hard dependency cycle and keeps opensearch.py importable
    # without the mappings module loaded at import time.
    from jawafdehi_shared.search.mappings import common_mappings, index_settings

    if client.indices.exists(index=name):
        return False
    body = {
        "settings": settings if settings is not None else index_settings(),
        "mappings": mappings if mappings is not None else common_mappings(),
    }
    client.indices.create(index=name, body=body)
    return True


def ensure_indices(client=None, names: tuple[str, ...] | list[str] | None = None) -> list[str]:
    """Ensure every unified-search index exists with the bilingual config.

    Idempotent: existing indices are left untouched. Returns the list of index
    names that were newly created. ``client`` defaults to ``make_client()``.
    """
    if client is None:
        client = make_client()
    created: list[str] = []
    for name in names or ALL_INDICES:
        if create_index(client, name):
            created.append(name)
    return created
