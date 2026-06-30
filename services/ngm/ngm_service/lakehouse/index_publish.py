"""R2 published-index JSON-LD shaping + publish (gold-zone public export).

The public crawl surface (the R2 index tree ported from the FastAPI
``ngm.index.build_index`` concept) emits schema.org JSON-LD: each manuscript /
leaf node is a CreativeWork-family JSON-LD object (``@context`` / ``@type`` /
``@id``), so the published archive is linked-data.

The NODE/MANUSCRIPT SHAPE is real and tested here (delegating to the single
source of truth in ``ngm_service.materials.jsonld``). :func:`publish_index_jsonld`
writes the shaped tree to the R2 (S3-compatible) gold bucket via boto3 — one
JSON-LD object per node, keyed by the node's index path. It is engine-agnostic
(R2/MinIO/S3, switched by env only) and testable without a live bucket: pass an
injected ``client`` (a boto3-S3-compatible stub). When R2 is not configured and
no client is injected it raises a clear ``RuntimeError`` (NOT a silent no-op).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from typing import Any

from ngm_service.lakehouse.config import LakehouseSettings, load_settings
from ngm_service.materials.jsonld import index_node_jsonld, manuscript_jsonld

logger = logging.getLogger("ngm.lakehouse.index_publish")

# Re-export the shaping functions so the index builder (and tests) treat this
# module as the lakehouse-facing seam for the published index's JSON-LD form.
__all__ = [
    "index_node_jsonld",
    "manuscript_jsonld",
    "node_tree_to_jsonld",
    "node_object_key",
    "publish_index_jsonld",
]

#: Key prefix + extension for published index objects in the gold bucket.
INDEX_KEY_PREFIX = "index"
JSONLD_EXT = ".jsonld"
JSONLD_CONTENT_TYPE = "application/ld+json"


def node_tree_to_jsonld(node: dict[str, Any]) -> dict[str, Any]:
    """Shape a whole index node tree (in dict form) into JSON-LD, recursively.

    Branch children that carry their own inline ``children``/``manuscripts`` are
    shaped in place; pure ``$ref`` stubs stay as link refs (resolved by fetching
    the child's own file). Pure function — no I/O — so the published tree's shape
    is fully testable without R2.
    """
    shaped = index_node_jsonld(node)
    # Recurse into any inline (non-$ref) children so a fully-materialized tree
    # shapes end-to-end; index_node_jsonld already handled the immediate level.
    inline_children = [
        c
        for c in (node.get("children") or [])
        if isinstance(c, dict) and not c.get("$ref") and (c.get("children") or c.get("manuscripts"))
    ]
    if inline_children:
        shaped["hasPart"] = [node_tree_to_jsonld(c) for c in inline_children] + [
            part
            for part in shaped.get("hasPart", [])
            # keep manuscript parts (CreativeWorks), drop the shallow child stubs
            # we just replaced with their fully-shaped form.
            if part.get("@type") not in ("CollectionPage",)
        ]
    return shaped


def node_object_key(shaped_node: dict[str, Any], *, fallback_index: int = 0) -> str:
    """The R2 object key for a shaped index node.

    Derived from the node's ``jawafdehi:indexPath`` (the stable tree path) so the
    published layout mirrors the index tree and re-publishing OVERWRITES the same
    keys (idempotent). The root path ``/`` maps to ``index/index.jsonld``; a path
    ``/court-orders/supreme`` maps to ``index/court-orders/supreme.jsonld``. A node
    with no usable path falls back to ``index/node-<n>.jsonld``.
    """
    path = (shaped_node.get("jawafdehi:indexPath") or "").strip()
    rel = path.strip("/")
    if not rel:
        rel = "index"
    return f"{INDEX_KEY_PREFIX}/{rel}{JSONLD_EXT}"


def _is_publishable_node(part: Any) -> bool:
    """True for a fully-shaped index node that should be written as its own file.

    A publishable node carries BOTH an ``@id`` (minted from its index path by
    ``index_node_jsonld``) AND a ``jawafdehi:indexPath``. This precisely excludes:
    * pure ``$ref`` child stubs — they carry ``jawafdehi:indexPath`` + a ``url``
      ref but NO ``@id`` (their full content lives in their own top-level file, so
      writing the stub here would clobber it at the same key); and
    * inlined manuscript CreativeWorks — they carry a material ``@id`` but NO
      ``jawafdehi:indexPath`` (they ride inside their leaf node's document).
    """
    return (
        isinstance(part, dict)
        and part.get("jawafdehi:indexPath") is not None
        and part.get("@id") is not None
    )


def _iter_shaped_nodes(
    shaped: dict[str, Any],
) -> Iterable[dict[str, Any]]:
    """Yield every fully-shaped, publishable node in a shaped tree.

    Walks ``hasPart`` recursively, emitting only nodes for which
    :func:`_is_publishable_node` holds (real branch/leaf nodes with their own
    ``@id`` + index path). ``$ref`` stubs and inlined manuscripts are not emitted
    as separate files (see :func:`_is_publishable_node`).
    """
    if not _is_publishable_node(shaped):
        return
    yield shaped
    for part in shaped.get("hasPart") or []:
        if _is_publishable_node(part):
            yield from _iter_shaped_nodes(part)


def _make_s3_client(settings: LakehouseSettings):
    """Build a boto3 S3 client for the R2/S3 object store from ``settings``.

    Engine-agnostic: the same call targets Cloudflare R2, MinIO, or AWS S3 — only
    env differs (endpoint/credentials/addressing). Path-style addressing is used
    for R2/MinIO. Raises ``RuntimeError`` if credentials are absent.
    """
    if not settings.s3.is_configured:
        raise RuntimeError(
            "Cannot publish the JSON-LD index: object-store credentials are not "
            "configured (set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY and, for "
            "R2/MinIO, S3_ENDPOINT_URL). Pass an injected client for tests."
        )
    import boto3  # lazy: keep import cost off the hot module path
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=settings.s3.endpoint_url,
        aws_access_key_id=settings.s3.access_key_id,
        aws_secret_access_key=settings.s3.secret_access_key,
        region_name=settings.s3.region,
        use_ssl=settings.s3.use_ssl,
        config=Config(s3={"addressing_style": settings.s3.url_style}),
    )


def publish_index_jsonld(
    nodes: Iterable[dict[str, Any]],
    *,
    settings: LakehouseSettings | None = None,
    client: Any = None,
    bucket: str | None = None,
) -> int:
    """Publish the JSON-LD index tree to the R2 gold bucket. Returns files written.

    Each top-level node in ``nodes`` is shaped via :func:`node_tree_to_jsonld`,
    then every node in the shaped tree that owns an index path is written as one
    ``application/ld+json`` object keyed by :func:`node_object_key` (idempotent —
    re-publishing overwrites the same keys). Leaf manuscripts ride inside their
    leaf node's document (not separate files).

    Engine-agnostic + testable:
    * ``client`` — inject a boto3-S3-compatible client (a stub in tests); when
      omitted one is built from ``settings`` (R2/MinIO/S3 by env).
    * ``bucket`` — overrides the target; defaults to the configured gold bucket.

    Raises ``RuntimeError`` if neither an injected client nor configured
    credentials are available, or if no target bucket can be resolved — a clear
    signal instead of a silent no-op.
    """
    settings = settings or load_settings()
    s3 = client if client is not None else _make_s3_client(settings)
    target_bucket = bucket or settings.gold_bucket
    if not target_bucket:
        raise RuntimeError(
            "Cannot publish the JSON-LD index: no gold bucket configured "
            f"(set {'NGM_GOLD_BUCKET'!r}) and none passed via bucket=."
        )

    written = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        shaped = node_tree_to_jsonld(node)
        for shaped_node in _iter_shaped_nodes(shaped):
            key = node_object_key(shaped_node)
            body = json.dumps(shaped_node, ensure_ascii=False).encode("utf-8")
            s3.put_object(
                Bucket=target_bucket,
                Key=key,
                Body=body,
                ContentType=JSONLD_CONTENT_TYPE,
            )
            written += 1
    logger.info(
        "published JSON-LD index tree to R2",
        extra={"bucket": target_bucket, "files_written": written},
    )
    return written
