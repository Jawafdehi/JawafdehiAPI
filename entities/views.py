"""NES entity API plane (DRF) — schema.org JSON-LD keyed by @id IRI.

CLEAN-SLATE remodel (2026-06-28): entities are stored as raw schema.org JSON-LD
keyed by their ``@id`` IRI (``https://jawafdehi.org/entity/<prefix>/<slug>``).
Every id in every request/response is that IRI.

All routes below are mounted under ``/api/`` (config.urls); the
paths shown are relative to that prefix.

- Read plane (public, ``AllowAny``):
    GET /api/entities             — list/search (+ filters), batch by ids
    GET /api/entities/{ref}       — detail (ref = url-encoded IRI OR prefix/slug)
    GET /api/entities/{ref}/versions
    GET /api/entities/tags
    GET /api/entity_prefixes
  List shape: ``{entities, total, limit, offset}``; detail: the stored JSON-LD doc.
  (Batch-by-ids returns ``{entities, total, requested, not_found}`` instead.)
- Write plane (OIDC + a content role via ``HasEntityWriteRole`` — Caseworker/Moderator/Admin):
    POST   /api/entities          — create (JSON-LD or authoring shape)
    PATCH  /api/entities/{ref}    — RFC-6902 jsonpatch (immutable @id/@type guarded)
    DELETE /api/entities/{ref}    — soft delete (is_deleted=True)
- Admin plane (OIDC + Moderator/Admin via ``HasEntityAdminRole``):
    POST  /api/admin/reindex      — reindex stub (no-op until search backend wired)

The detail ``{ref}`` is resolved to the canonical @id IRI by ``_resolve_ref``:
a value already starting with ``http`` is taken as the IRI; otherwise it is the
``<prefix>/<slug>`` path and the IRI is built from it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.parse import quote, unquote

import jsonpatch
from jawafdehi_shared.drf.auditlog import AuditlogActorMixin
from jawafdehi_shared.entities.ids import (
    build_entity_iri,
    canonicalize_entity_iri,
    is_valid_entity_iri,
)
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from entities.permissions import HasEntityAdminRole, HasEntityWriteRole
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
from entities.services.merge import EntityMergeService, MergeError

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


def _redirect_to_survivor(iri: str, *, suffix: str = "") -> Optional[Response]:
    """A 301 to the survivor if ``iri`` is a merge tombstone, else None."""
    repo = EntityRepository()
    target = repo.resolve_tombstone(iri)
    # Guard that the target is live, the same way the batch lookup does: a survivor
    # later soft-deleted would otherwise redirect into a 404.
    if not target or repo.get_entity(target) is None:
        return None
    location = f"/api/entities/{quote(target, safe='')}{suffix}"
    response = Response(status=status.HTTP_301_MOVED_PERMANENTLY)
    response["Location"] = location
    return response


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    # This is the platform-wide health route (mounted at /api/health for the whole
    # unified monolith, not just the entities/NES app). The "nes-api" service name
    # was the pre-unification identity; the canonical name is now "jawafdehi-api".
    return Response({"status": "ok", "service": "jawafdehi-api"})


# ---------------------------------------------------------------------------
# Read + write plane: list / create
# ---------------------------------------------------------------------------


class EntityListCreateView(AuditlogActorMixin, APIView):
    """GET /api/entities (public list/search/batch) + POST /api/entities (create).

    ``AuditlogActorMixin`` attributes any audit entry to the authenticated user;
    the seam is kept uniform across the platform's write views."""

    def get_permissions(self):
        if self.request.method == "POST":
            return [HasEntityWriteRole()]
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
        found, not_found, redirected = [], [], {}
        seen_ids = set()
        for ref in refs:
            iri = _resolve_ref(ref)
            doc = repo.get_entity(iri) if iri else None
            if doc is None and iri:
                target = repo.resolve_tombstone(iri)
                if target:
                    doc = repo.get_entity(target)
                    if doc is not None:
                        redirected[iri] = target
            if doc is None:
                not_found.append(ref)
            elif doc["@id"] not in seen_ids:
                seen_ids.add(doc["@id"])
                found.append(doc)
        body: Dict[str, Any] = {
            "entities": found,
            "total": len(found),
            "requested": len(refs),
        }
        if not_found:
            body["not_found"] = not_found
        if redirected:
            body["redirected"] = redirected
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


class EntityDetailView(AuditlogActorMixin, APIView):
    """GET /api/entities/{ref} (public) + PATCH/DELETE /api/entities/{ref} (write).

    DELETE is a SOFT delete: it flips ``is_deleted=True`` so the entity vanishes
    from the read plane (list/detail/search) but is never hard-removed — this is
    an accountability/audit platform.
    """

    def get_permissions(self):
        if self.request.method in ("PATCH", "DELETE"):
            return [HasEntityWriteRole()]
        return [AllowAny()]

    def get(self, request, ref: str):
        iri = _resolve_ref(ref)
        doc = EntityRepository().get_entity(iri) if iri else None
        if doc is None:
            if iri:
                redirect = _redirect_to_survivor(iri)
                if redirect is not None:
                    return redirect
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

    def delete(self, request, ref: str):
        """Soft-delete the entity (``is_deleted=True``); returns 204.

        A soft-deleted entity disappears from the read plane but its row (and
        version history) is preserved. Deleting an unknown/already-deleted IRI is
        a 404 — the entity is not visible to this caller either way.
        """
        iri = _resolve_ref(ref)
        if iri is None:
            return Response(
                _err("INVALID_ENTITY_ID", f"Invalid entity reference: {ref}"),
                status=status.HTTP_400_BAD_REQUEST,
            )
        service = PublicationService()
        deleted = service.delete_entity(
            iri,
            author_id=_author_id_from_request(request),
            change_description="Deleted via API",
        )
        if not deleted:
            return Response(
                _err("NOT_FOUND", f"Entity {iri} not found"),
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


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
        if EntityRepository().get_entity(iri) is None:
            redirect = _redirect_to_survivor(iri, suffix="/versions")
            if redirect is not None:
                return redirect
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


class EntityMergeView(AuditlogActorMixin, APIView):
    """POST /api/entities/merge — fold duplicate entities into one survivor.

    See docs/superpowers/specs/2026-08-11-entities-merge-api-spec.md for the contract.
    """

    permission_classes = [HasEntityWriteRole]

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        dry_run = body.get("dry_run", False)
        if not isinstance(dry_run, bool):
            return Response(
                _err("INVALID_REQUEST", "dry_run must be a boolean."),
                status=status.HTTP_400_BAD_REQUEST,
            )
        type_family = body.get("type_family")
        if type_family is not None and not isinstance(type_family, str):
            return Response(
                _err("INVALID_REQUEST", "type_family must be a string."),
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            result = EntityMergeService().merge(
                survivor_iri=body.get("survivor", ""),
                duplicate_iris=body.get("duplicates", []),
                author_id=_author_id_from_request(request),
                change_description=body.get("change_description", ""),
                type_family=type_family,
                dry_run=dry_run,
            )
        except MergeError as exc:
            payload = _err(exc.code, exc.message)
            payload["error"].update(exc.extra)
            # The spec pairs a 500 MERGE_INCOMPLETE with status "partial".
            if exc.code == "MERGE_INCOMPLETE":
                payload["status"] = "partial"
            return Response(payload, status=exc.http_status)
        return Response(result, status=status.HTTP_200_OK)


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
    """POST /api/admin/reindex — OIDC + Moderator/Admin gated stub."""

    permission_classes = [HasEntityAdminRole]

    def post(self, request):
        # Reindexing the unified OpenSearch index is a bulk, potentially
        # long-running operation, so it is NOT run synchronously inside this
        # request. It is driven out-of-band by the ``reindex_all`` /
        # ``reindex_entities`` management commands (see search/management/
        # commands/). This endpoint acknowledges the request without performing
        # the reindex itself.
        return Response(
            {
                "status": "not_run",
                "detail": (
                    "Reindexing is performed out-of-band via the reindex_all / "
                    "reindex_entities management commands, not synchronously here."
                ),
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
