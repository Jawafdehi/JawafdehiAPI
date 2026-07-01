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

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from jawafdehi_shared.entities.ids import (
    build_material_iri,
    is_valid_material_iri,
    iri_base,
)
from courts.permissions import HasNgmRole

from . import jsonld
from .bulk_ingest import _infer_material_type
from .models import Material

LD_JSON = "application/ld+json"


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
    """Return the JSON-LD doc for ``iri``: stored row, else a derived court case."""
    try:
        row = Material.objects.get(pk=iri)
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
            court_id=court_identifier, case_number__iexact=case_number
        ).first()
    )
    if case is None:
        return None
    return jsonld.court_case_to_jsonld(case)


@api_view(["GET", "PUT"])
@permission_classes([AllowAny])
def material_detail(request, source: str, ident: str):
    """``GET /api/materials/<source>/<ident>`` → the material JSON-LD doc.

    ``PUT`` replaces the material's stored JSON-LD (NGM-role gated). The GET path
    is byte-identical to before (stored row, else on-the-fly court-case fallback).
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

    data = _resolve_material(iri)
    if data is None:
        return Response({"detail": "Material not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(data, content_type=LD_JSON)


@api_view(["GET", "POST"])
@permission_classes([AllowAny])
def material_by_iri(request):
    """``GET /api/materials/?iri=<full-iri>`` → the material JSON-LD doc.

    Accepts either the canonical full IRI or a bare ``/material/<source>/<ident>``
    path; the latter is resolved against the platform ``iri_base()``.

    ``POST`` creates/upserts a material from a JSON-LD body (NGM-role gated). The
    body is either a bare JSON-LD doc (``@id`` present) or the
    ``{"material": {...}, "material_type": "..."}`` envelope; ``material_type`` is
    inferred from the doc's ``@type`` when absent (mirroring bulk_ingest).
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
    if not iri:
        return Response(
            {"detail": "Query parameter 'iri' is required."},
            status=status.HTTP_422_UNPROCESSABLE_ENTITY,
        )
    if iri.startswith("/material/"):
        iri = f"{iri_base()}{iri}"
    if not is_valid_material_iri(iri):
        return Response(
            {"detail": "Not a valid material @id IRI."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    data = _resolve_material(iri)
    if data is None:
        return Response({"detail": "Material not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(data, content_type=LD_JSON)
