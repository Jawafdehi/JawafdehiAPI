"""Shared OpenSearch client helpers for the platform's bilingual entity/document
search (the PR-91 substrate, reused by NES entity search and NGM text search).

This is a thin, framework-agnostic wrapper so each Django service configures one
client from its settings rather than duplicating connection/index code. The
heavy document-builder + EN<->Devanagari transliteration logic lives with NES
(it owns the entity index); this module standardizes connection + index naming.
"""

from __future__ import annotations

import os
from typing import Any


def get_opensearch_url() -> str:
    """Resolve the shared OpenSearch endpoint from env (OPENSEARCH_URL)."""
    return os.getenv("OPENSEARCH_URL", "http://localhost:9200")


def make_client(url: str | None = None):
    """Construct an opensearch-py client. Imported lazily so the package is
    importable without the optional dependency installed (e.g. in a service that
    doesn't use search).

    Auth from env: if ``OPENSEARCH_USER`` / ``OPENSEARCH_PASSWORD`` are set
    (self-hosted with the security plugin enabled), HTTP basic auth is used and
    TLS verification is left on. With no creds (dev compose, security disabled)
    the client connects anonymously.
    """
    from opensearchpy import OpenSearch  # lazy: optional dependency

    user = os.getenv("OPENSEARCH_USER")
    password = os.getenv("OPENSEARCH_PASSWORD")
    kwargs: dict[str, Any] = {"hosts": [url or get_opensearch_url()]}
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
