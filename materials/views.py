"""Material read plane: serve the schema.org JSON-LD for a material.

``GET /api/materials/<source>/<ident>`` (the material IRI's path component) or
``GET /api/materials/?iri=<full-iri>`` returns the stored JSON-LD document
(``application/ld+json``-shaped). Materials are derived from public-domain
government documents, so reads are public.

If a stored ``Material`` row exists it is served verbatim. As a clean-slate
convenience, a court-case material IRI (``/material/court/<court>.<case>``) with
no stored row is materialized on the fly from the relational court tables via
``jsonld.court_case_to_jsonld`` — so the court read plane can hand out a material
``@id`` that always resolves.
"""

from __future__ import annotations

import mimetypes

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.decorators import (
    api_view,
    parser_classes,
    permission_classes,
)
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from jawafdehi_shared.entities.ids import (
    build_material_iri,
    is_valid_material_iri,
    iri_base,
)
from jawafdehi_shared.drf.base import PlatformCursorPagination
from jawafdehi_shared.storage import store_file_as_link
from courts.permissions import HasNgmRole

from . import jsonld
from .bulk_ingest import _infer_material_type
from .models import Material

LD_JSON = "application/ld+json"

#: Roles a file upload may carry (mirrors the DocumentSource link roles that the
#: JSON-LD MediaObject mapping understands — jsonld._ROLE_ENCODING_HINTS).
_UPLOAD_ROLES = frozenset({"RAW", "ALTERNATE", "PERMALINK"})


def _require_ngm_role(request):
    """Enforce ``HasNgmRole`` inside an ``AllowAny`` function view.

    These materials endpoints are function-based ``@api_view`` (so GET stays
    public and byte-identical); the write methods enforce the NGM-role gate
    manually here. Returns a 401/403 ``Response`` to short-circuit, or ``None``
    when the principal is allowed — mirroring DRF's contract: unauthenticated is
    401 (the OIDC authenticator sets WWW-Authenticate), authenticated-without-role
    is 403.
    """
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated):
        return Response(
            {"detail": "Authentication credentials were not provided."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    if not HasNgmRole().has_permission(request, None):
        return Response({"detail": HasNgmRole.message}, status=status.HTTP_403_FORBIDDEN)
    return None


def _upsert_material(data, *, material_type: str | None, expected_iri: str | None = None):
    """Validate + upsert a Material from a JSON-LD body. Returns (doc, status, error).

    On success: ``(doc, 200_or_201, None)``. On validation failure:
    ``(None, 400, {"detail": ...})``. ``material_type`` is inferred from the doc's
    @type when not supplied (mirroring bulk_ingest). When ``expected_iri`` is set
    (the PUT path) the body's @id must match the URL's IRI.
    """
    if not isinstance(data, dict) or "@id" not in data:
        return None, status.HTTP_400_BAD_REQUEST, {
            "detail": "Body must be a JSON-LD object with an '@id'."
        }
    if expected_iri is not None and data.get("@id") != expected_iri:
        return None, status.HTTP_400_BAD_REQUEST, {
            "detail": f"Body @id {data.get('@id')!r} must match the URL IRI {expected_iri!r}."
        }
    mtype = material_type or _infer_material_type(data)
    try:
        # from_jsonld validates the doc + derives the promoted columns from @id.
        built = Material.from_jsonld(data, material_type=mtype)
        existing = Material.objects.filter(pk=built.iri).first()
        if existing is not None:
            # Replace the existing row in place so auto-managed columns
            # (created_at) are preserved and Django issues an UPDATE.
            existing.material_type = built.material_type
            existing.source = built.source
            existing.ident = built.ident
            existing.data = built.data
            # A (re-)upsert revives a soft-deleted row: the write is the source
            # of truth, so clear the soft-delete flag.
            existing.is_deleted = False
            material = existing
        else:
            material = built
        # validate_unique=False: an upsert intentionally targets an existing @id
        # (the PK); the iri/source/ident/data checks in clean() still run.
        material.full_clean(validate_unique=False)
        material.save()
    except (ValueError, DjangoValidationError) as exc:
        detail = exc.message_dict if isinstance(exc, DjangoValidationError) else str(exc)
        return None, status.HTTP_400_BAD_REQUEST, {"detail": detail}
    code = status.HTTP_200_OK if existing is not None else status.HTTP_201_CREATED
    return material.data, code, None


def _resolve_material(iri: str) -> dict | None:
    """Return the JSON-LD doc for ``iri``: stored (live) row, else a derived
    court case. Soft-deleted rows (``is_deleted=True``) are treated as absent."""
    try:
        row = Material.objects.get(pk=iri, is_deleted=False)
        return row.data
    except Material.DoesNotExist:
        pass
    return _derive_court_case_jsonld(iri)


def _derive_court_case_jsonld(iri: str) -> dict | None:
    """Clean-slate fallback: build court-case JSON-LD from the relational tables.

    A material IRI of the form ``/material/court/<court_identifier>.<case_number>``
    is reconstructed from the live court tables when no ``Material`` row exists.
    """
    from jawafdehi_shared.entities.ids import parse_material_iri

    try:
        parsed = parse_material_iri(iri)
    except ValueError:
        return None
    if parsed.source != jsonld.COURT_SOURCE or "." not in parsed.ident:
        return None
    court_identifier, _, case_number = parsed.ident.partition(".")

    from courts.models import CourtCase

    # ident is lowercased in the IRI; the case_number on disk is uppercased.
    case = (
        CourtCase.objects.filter(
            court_id=court_identifier,
            case_number__iexact=case_number,
            is_deleted=False,
        ).first()
    )
    if case is None:
        return None
    return jsonld.court_case_to_jsonld(case)


def _soft_delete_material(iri: str) -> bool:
    """Soft-delete a stored material (``is_deleted=True``); never hard-delete.

    Returns True iff a live stored row was flipped. Derived (court-case) materials
    have no stored row and cannot be deleted here → False (404 for the caller).
    """
    row = Material.objects.filter(pk=iri, is_deleted=False).first()
    if row is None:
        return False
    row.is_deleted = True
    row.save(update_fields=["is_deleted", "updated_at"])
    return True


@api_view(["GET", "PUT", "DELETE"])
@permission_classes([AllowAny])
def material_detail(request, source: str, ident: str):
    """``GET /api/materials/<source>/<ident>`` → the material JSON-LD doc.

    ``PUT`` replaces the material's stored JSON-LD (NGM-role gated). ``DELETE``
    soft-deletes the stored material (NGM-role gated, 204). The GET path is
    byte-identical to before (stored row, else on-the-fly court-case fallback).
    """
    try:
        iri = build_material_iri(source, ident)
    except ValueError:
        return Response(
            {"detail": "Invalid material source/ident."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if request.method == "PUT":
        denied = _require_ngm_role(request)
        if denied is not None:
            return denied
        body = request.data if isinstance(request.data, dict) else {}
        material_type = body.get("material_type") if isinstance(body, dict) else None
        doc = body.get("material") if isinstance(body, dict) and "material" in body else body
        result, code, error = _upsert_material(
            doc, material_type=material_type, expected_iri=iri
        )
        if error is not None:
            return Response(error, status=code)
        return Response(result, status=code, content_type=LD_JSON)

    if request.method == "DELETE":
        denied = _require_ngm_role(request)
        if denied is not None:
            return denied
        if not _soft_delete_material(iri):
            return Response(
                {"detail": "Material not found."}, status=status.HTTP_404_NOT_FOUND
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    data = _resolve_material(iri)
    if data is None:
        return Response({"detail": "Material not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(data, content_type=LD_JSON)


def _append_media_object(doc: dict, *, content_url: str, role: str, filename: str) -> dict:
    """Append a schema.org ``MediaObject`` for an uploaded file to ``associatedMedia``.

    Reuses the ``jsonld._media_object`` shaping (``contentUrl`` /
    ``jawafdehi:linkRole`` / ``encodingFormat``) so an uploaded file is one more
    roled link on the material — identical to the DocumentSource modality. The
    ``encodingFormat`` is guessed from the filename extension (the roled-link
    hint has no format for RAW/ALTERNATE/PERMALINK). Mutates + returns ``doc``.
    """
    mo = jsonld._media_object({"link": content_url, "role": role})
    if mo is not None:
        encoding, _ = mimetypes.guess_type(filename)
        if encoding and "encodingFormat" not in mo:
            mo["encodingFormat"] = encoding
        media = doc.get("associatedMedia")
        if isinstance(media, dict):
            media = [media]
        elif not isinstance(media, list):
            media = []
        media.append(mo)
        doc["associatedMedia"] = media
    return doc


@api_view(["POST"])
@permission_classes([AllowAny])
@parser_classes([MultiPartParser, FormParser])
def material_file_upload(request, source: str, ident: str):
    """``POST /api/materials/<source>/<ident>/file`` (multipart) — attach a file.

    Streams the uploaded ``file`` to object storage (the SAME hashed-filename S3
    mechanism the cases app uses, via ``jawafdehi_shared.storage``), then upserts
    the material at ``@id=/material/<source>/<ident>``: the stored file's public
    URL is appended to ``associatedMedia`` as a schema.org ``MediaObject``
    (``contentUrl`` + ``jawafdehi:linkRole`` = ``role`` + guessed
    ``encodingFormat``). If no material exists yet it is CREATED (which requires
    ``material_type``); an existing material is UPDATED in place.

    Multipart fields: ``file`` (required binary), ``role`` (RAW|ALTERNATE|PERMALINK,
    default RAW), ``material_type`` (required only when creating a fresh material).
    NGM-role gated. Returns the material JSON-LD (201 created / 200 updated).
    """
    denied = _require_ngm_role(request)
    if denied is not None:
        return denied

    try:
        iri = build_material_iri(source, ident)
    except ValueError:
        return Response(
            {"detail": "Invalid material source/ident."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    uploaded = request.FILES.get("file")
    if uploaded is None:
        return Response(
            {"detail": "A multipart 'file' is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    role = (request.data.get("role") or "RAW").strip().upper()
    if role not in _UPLOAD_ROLES:
        return Response(
            {"detail": f"role must be one of {sorted(_UPLOAD_ROLES)}; got {role!r}."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    existing = Material.objects.filter(pk=iri, is_deleted=False).first()
    if existing is None:
        material_type = (request.data.get("material_type") or "").strip()
        if not material_type:
            return Response(
                {
                    "detail": (
                        "material_type is required to create a new material "
                        "(no material exists at this @id yet)."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        schema_type, additional_type = jsonld.type_for(material_type)
        doc = {
            "@context": jsonld.MATERIAL_CONTEXT,
            "@type": schema_type,
            "@id": iri,
            "name": {"ne": uploaded.name},
        }
        if additional_type:
            doc["additionalType"] = additional_type
    else:
        material_type = existing.material_type
        doc = dict(existing.data)

    # Stream the file to storage (shared hashed-filename S3 mechanism) and append
    # its permanent URL as a roled MediaObject on the material.
    link = store_file_as_link(uploaded, role=role)
    _append_media_object(
        doc, content_url=link["link"], role=link["role"], filename=uploaded.name
    )

    result, code, error = _upsert_material(
        doc, material_type=material_type, expected_iri=iri
    )
    if error is not None:
        return Response(error, status=code)
    return Response(result, status=code, content_type=LD_JSON)


def _normalize_iri_param(iri: str) -> str:
    """Resolve a bare ``/material/<source>/<ident>`` path against ``iri_base()``."""
    if iri.startswith("/material/"):
        return f"{iri_base()}{iri}"
    return iri


def _list_materials(request) -> Response:
    """``GET /api/materials/`` (no ``iri``) → paginated list of live materials.

    Uses the shared ``PlatformCursorPagination`` so the wire shape is the
    platform ``{results, next}`` (matching the courts list plane). Soft-deleted
    rows are excluded. Optional ``source`` / ``material_type`` query filters
    mirror the promoted-column filters used elsewhere.
    """
    qs = Material.objects.filter(is_deleted=False)
    params = request.query_params
    if (source := params.get("source")):
        qs = qs.filter(source=source)
    if (material_type := params.get("material_type")):
        qs = qs.filter(material_type=material_type)

    paginator = PlatformCursorPagination()
    page = paginator.paginate_queryset(qs, request)
    docs = [row.data for row in page]
    return paginator.get_paginated_response(docs)


@api_view(["GET", "POST", "DELETE"])
@permission_classes([AllowAny])
def material_by_iri(request):
    """``GET /api/materials/`` → paginated list of materials (``{results, next}``);
    ``GET /api/materials/?iri=<full-iri>`` → a single material JSON-LD doc.

    The ``iri`` form accepts either the canonical full IRI or a bare
    ``/material/<source>/<ident>`` path (resolved against ``iri_base()``).

    ``POST`` creates/upserts a material from a JSON-LD body (NGM-role gated); the
    body is either a bare JSON-LD doc (``@id`` present) or the
    ``{"material": {...}, "material_type": "..."}`` envelope (``material_type`` is
    inferred from the doc's ``@type`` when absent, mirroring bulk_ingest).
    ``DELETE /api/materials/?iri=<full-iri>`` soft-deletes the material (NGM-role
    gated, 204).
    """
    if request.method == "POST":
        denied = _require_ngm_role(request)
        if denied is not None:
            return denied
        body = request.data if isinstance(request.data, dict) else {}
        material_type = body.get("material_type") if isinstance(body, dict) else None
        doc = body.get("material") if isinstance(body, dict) and "material" in body else body
        result, code, error = _upsert_material(doc, material_type=material_type)
        if error is not None:
            return Response(error, status=code)
        return Response(result, status=code, content_type=LD_JSON)

    iri = (request.query_params.get("iri") or "").strip()

    if request.method == "DELETE":
        denied = _require_ngm_role(request)
        if denied is not None:
            return denied
        if not iri:
            return Response(
                {"detail": "Query parameter 'iri' is required."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        iri = _normalize_iri_param(iri)
        if not is_valid_material_iri(iri):
            return Response(
                {"detail": "Not a valid material @id IRI."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not _soft_delete_material(iri):
            return Response(
                {"detail": "Material not found."}, status=status.HTTP_404_NOT_FOUND
            )
        return Response(status=status.HTTP_204_NO_CONTENT)

    # GET: no iri → paginated list; iri → single lookup (unchanged behavior).
    if not iri:
        return _list_materials(request)
    iri = _normalize_iri_param(iri)
    if not is_valid_material_iri(iri):
        return Response(
            {"detail": "Not a valid material @id IRI."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    data = _resolve_material(iri)
    if data is None:
        return Response({"detail": "Material not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(data, content_type=LD_JSON)
