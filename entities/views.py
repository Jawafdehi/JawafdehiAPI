"""NES entity API plane (DRF) — schema.org JSON-LD keyed by @id IRI.

CLEAN-SLATE remodel (2026-06-28): entities are stored as raw schema.org JSON-LD
keyed by their ``@id`` IRI (``https://jawafdehi.org/entity/<prefix>/<slug>``).
Every id in every request/response is that IRI.

All routes below are mounted under ``/api/nes/`` (config.urls); the
paths shown are relative to that prefix.

- Read plane (public, ``AllowAny``):
    GET /api/nes/entities             — list/search (+ filters), batch by ids
    GET /api/nes/entities/{ref}       — detail (ref = url-encoded IRI OR prefix/slug)
    GET /api/nes/entities/{ref}/versions
    GET /api/nes/entities/tags
    GET /api/nes/entity_prefixes
  List shape: ``{entities, total, limit, offset}``; detail: the stored JSON-LD doc.
  (Batch-by-ids returns ``{entities, total, requested, not_found}`` instead.)
- Write plane (OIDC + ``nes_contributor`` via ``HasNesContributorRole``):
    POST  /api/nes/entities           — create (JSON-LD or authoring shape)
    PATCH /api/nes/entities/{ref}     — RFC-6902 jsonpatch (immutable @id/@type guarded)
- Admin plane (OIDC + ``nes_admin`` via ``HasNesAdminRole``):
    POST  /api/nes/admin/reindex      — reindex stub (no-op until search backend wired)

The detail ``{ref}`` is resolved to the canonical @id IRI by ``_resolve_ref``:
a value already starting with ``http`` is taken as the IRI; otherwise it is the
``<prefix>/<slug>`` path and the IRI is built from it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import unquote

import jsonpatch
from jawafdehi_shared.entities.ids import (
    build_entity_iri,
    canonicalize_entity_iri,
    is_valid_entity_iri,
    parse_entity_iri,
)
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from entities.permissions import HasNesAdminRole, HasNesContributorRole
from entities.persistence import (
    EntityRepository,
    _clamp_limit,
    _clamp_offset,
)
from entities.validation import JsonLdValidationError, validate_jsonld_entity
from entities.write_validation import (
    normalize_patch_ops,
    validate_create_payload,
)
from entities.services.publication import PublicationService

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 25


def _err(code: str, message: str) -> Dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _author_id_from_request(request) -> str:
    sub = getattr(getattr(request, "user", None), "username", None) or "unknown"
    return f"oidc:{sub}"


def _resolve_ref(ref: str) -> Optional[str]:
    """Resolve a detail-route ``{ref}`` to a canonical entity @id IRI, or None.

    Accepts either a url-encoded full IRI (``https://.../entity/person/x``) or the
    bare ``<prefix>/<slug>`` path. Returns the canonical IRI, or None if neither
    form is a valid entity IRI. A valid-shaped full IRI on a non-canonical host
    is re-keyed onto the canonical authority so the lookup hits the same PK the
    store wrote (host is part of the join key).
    """
    ref = unquote(ref or "")
    if ref.startswith("http://") or ref.startswith("https://"):
        try:
            return canonicalize_entity_iri(ref)
        except ValueError:
            return None
    # Treat as <prefix>/<slug>: split off the final segment as the slug.
    if "/" not in ref:
        return None
    prefix, _, slug = ref.rpartition("/")
    try:
        iri = build_entity_iri(prefix, slug)
    except ValueError:
        return None
    return iri if is_valid_entity_iri(iri) else None


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    return Response({"status": "ok", "service": "nes-api"})


# ---------------------------------------------------------------------------
# Read + write plane: list / create
# ---------------------------------------------------------------------------


class EntityListCreateView(APIView):
    """GET /api/entities (public list/search/batch) + POST /api/entities (create)."""

    def get_permissions(self):
        if self.request.method == "POST":
            return [HasNesContributorRole()]
        return [AllowAny()]

    # --- GET: list / search / batch -----------------------------------
    def get(self, request):
        params = request.query_params
        repo = EntityRepository()

        ids = params.get("ids")
        query = params.get("query")
        entity_type = params.get("entity_type")
        entity_prefix = params.get("entity_prefix")
        keywords = params.get("keywords") or params.get("tags")
        limit = _clamp_limit(_int_param(params.get("limit"), 100))
        offset = _clamp_offset(_int_param(params.get("offset"), 0))

        if ids is not None:
            other = [query, entity_type, entity_prefix, keywords,
                     limit != 100, offset != 0]
            if any(other):
                return Response(
                    _err("INVALID_REQUEST",
                         "The 'ids' parameter cannot be combined with other parameters"),
                    status=status.HTTP_400_BAD_REQUEST,
                )
            return self._batch_lookup(ids, repo)

        keywords_list: Optional[List[str]] = None
        if keywords:
            keywords_list = [t.strip() for t in keywords.split(",") if t.strip()] or None

        entities = repo.search_entities(
            query=query,
            entity_type=entity_type,
            prefix=entity_prefix,
            keywords=keywords_list,
            limit=limit,
            offset=offset,
        )
        if query:
            total = len(entities)
        else:
            total = repo.count_search(
                entity_type=entity_type,
                prefix=entity_prefix,
                keywords=keywords_list,
            )

        return Response(
            {
                "entities": entities,
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        )

    def _batch_lookup(self, ids: str, repo: EntityRepository) -> Response:
        # IRIs contain commas only when url-encoded as %2C, so a plain comma is a
        # safe batch separator for the canonical IRI form.
        refs = [e.strip() for e in ids.split(",") if e.strip()]
        if not refs:
            return Response(
                _err("INVALID_REQUEST", "At least one entity ID is required"),
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(refs) > MAX_BATCH_SIZE:
            return Response(
                _err("BATCH_SIZE_EXCEEDED",
                     f"Maximum batch size is {MAX_BATCH_SIZE}. Requested: {len(refs)}"),
                status=status.HTTP_400_BAD_REQUEST,
            )
        found, not_found = [], []
        for ref in refs:
            iri = _resolve_ref(ref)
            doc = repo.get_entity(iri) if iri else None
            if doc is None:
                not_found.append(ref)
            else:
                found.append(doc)
        body: Dict[str, Any] = {
            "entities": found,
            "total": len(found),
            "requested": len(refs),
        }
        if not_found:
            body["not_found"] = not_found
        return Response(body)

    # --- POST: create --------------------------------------------------
    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        change_description = body.get("change_description", "Created via API")
        try:
            doc = validate_create_payload(body)
        except (ValueError, JsonLdValidationError) as e:
            return Response(
                _err("VALIDATION_ERROR", str(e)),
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        try:
            created = PublicationService().create_entity(
                doc=doc,
                author_id=_author_id_from_request(request),
                change_description=change_description,
            )
        except ValueError as e:
            return _map_service_value_error(e)
        return Response(created, status=status.HTTP_201_CREATED)


class EntityDetailView(APIView):
    """GET /api/entities/{ref} (public) + PATCH /api/entities/{ref} (write)."""

    def get_permissions(self):
        if self.request.method == "PATCH":
            return [HasNesContributorRole()]
        return [AllowAny()]

    def get(self, request, ref: str):
        iri = _resolve_ref(ref)
        doc = EntityRepository().get_entity(iri) if iri else None
        if doc is None:
            return Response(
                _err("NOT_FOUND", f"Entity {ref} not found"),
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(doc, content_type="application/ld+json")

    def patch(self, request, ref: str):
        iri = _resolve_ref(ref)
        if iri is None:
            return Response(
                _err("INVALID_ENTITY_ID", f"Invalid entity reference: {ref}"),
                status=status.HTTP_400_BAD_REQUEST,
            )
        body = request.data if isinstance(request.data, dict) else {}
        change_description = body.get("change_description", "Updated via API")
        try:
            patch_ops = normalize_patch_ops(body.get("patch_ops"))
        except ValueError as e:
            return Response(
                _err("VALIDATION_ERROR", str(e)),
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        service = PublicationService()
        doc = service.get_entity(iri)
        if doc is None:
            return Response(
                _err("NOT_FOUND", f"Entity {iri} not found"),
                status=status.HTTP_404_NOT_FOUND,
            )

        try:
            patched = jsonpatch.apply_patch(doc, patch_ops, in_place=False)
        except (jsonpatch.JsonPatchException, jsonpatch.JsonPointerException) as exc:
            return Response(
                _err("INVALID_PATCH", f"Invalid JSON Patch document: {exc}"),
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            validate_jsonld_entity(patched)
        except JsonLdValidationError as exc:
            return Response(
                _err("VALIDATION_ERROR", f"Patched entity is invalid: {exc}"),
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        try:
            updated = service.update_entity(
                doc=patched,
                author_id=_author_id_from_request(request),
                change_description=change_description,
            )
        except ValueError as e:
            return _map_service_value_error(e)
        return Response(updated)


class EntityVersionsView(APIView):
    """GET /api/entities/{ref}/versions (public)."""

    permission_classes = [AllowAny]

    def get(self, request, ref: str):
        iri = _resolve_ref(ref)
        if iri is None:
            return Response(
                _err("INVALID_ENTITY_ID", f"Invalid entity reference: {ref}"),
                status=status.HTTP_400_BAD_REQUEST,
            )
        limit = _clamp_limit(_int_param(request.query_params.get("limit"), 100))
        offset = _clamp_offset(_int_param(request.query_params.get("offset"), 0))
        service = PublicationService()
        versions = service.get_entity_versions(iri, limit=limit, offset=offset)
        return Response(
            {
                "versions": versions,
                "total": service.count_entity_versions(iri),
                "limit": limit,
                "offset": offset,
            }
        )


@api_view(["GET"])
@permission_classes([AllowAny])
def list_tags(request):
    return Response({"tags": EntityRepository().all_keywords()})


@api_view(["GET"])
@permission_classes([AllowAny])
def list_entity_prefixes(request):
    return Response({"prefixes": EntityRepository().all_prefixes()})


# ---------------------------------------------------------------------------
# Admin plane (stub)
# ---------------------------------------------------------------------------


class ReindexView(APIView):
    """POST /api/nes/admin/reindex — OIDC + ``nes_admin`` gated stub."""

    permission_classes = [HasNesAdminRole]

    def post(self, request):
        return Response(
            {
                "status": "skipped",
                "detail": "No search backend configured; search is served from the database.",
                "requested_by": getattr(request.user, "username", None),
            }
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _int_param(value: Any, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def _map_service_value_error(exc: ValueError) -> Response:
    message = str(exc)
    lowered = message.lower()
    if "already exists" in lowered:
        return Response(_err("ENTITY_EXISTS", message), status=status.HTTP_409_CONFLICT)
    if "not found" in lowered or "does not exist" in lowered:
        return Response(_err("NOT_FOUND", message), status=status.HTTP_404_NOT_FOUND)
    return Response(_err("INVALID_REQUEST", message), status=status.HTTP_400_BAD_REQUEST)
