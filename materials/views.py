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

import logging
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
from courts.permissions import NGM_ROLE_GROUPS, HasNgmRole

from . import jsonld
from . import provenance
from .models import Material, Policy
from .single_source_ingest import upsert_single_source_material

LD_JSON = "application/ld+json"

#: Roles a file upload may carry (the full material link-role vocab that the
#: JSON-LD MediaObject mapping understands — jsonld._ROLE_ENCODING_HINTS).
#: MARKDOWN + SOURCE_PAGE added per ADR (cases-own-no-documents, D-D) so no
#: legacy source link role is lost when sources fold into materials.
_UPLOAD_ROLES = frozenset(
    {"RAW", "ALTERNATE", "PERMALINK", "MARKDOWN", "SOURCE_PAGE"}
)

#: Upload roles that carry an original document worth OCR-ing into full text.
#: Excludes MARKDOWN (our own extraction output) + SOURCE_PAGE (HTML, not a scan).
_CONVERTIBLE_ROLES = frozenset({"RAW", "ALTERNATE", "PERMALINK"})

logger = logging.getLogger("materials.views")

#: Upper bound on an uploaded material file. Generous (scanned court orders /
#: charge sheets run large — this is NOT the 10 MB case-evidence limit) but
#: bounded so an NGM-role client can't stream an unbounded body into storage.
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


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


# Django Groups whose members may READ non-public (draft-only) materials: the
# org-wide ReadOnly read role plus the NGM-capable content role(s) that
# ``HasNgmRole`` gates (Caseworker / any future NGM tier). Derived from
# ``NGM_ROLE_GROUPS`` so that set stays the single source of truth. Writes stay
# gated separately — ``HasNgmRole`` / ``HasContributorRole`` exclude ReadOnly.
_NONPUBLIC_READ_GROUPS = frozenset({"ReadOnly"}) | NGM_ROLE_GROUPS


def _can_see_nonpublic(request) -> bool:
    """True iff the principal may see PRIVATE (draft-only) materials.

    Case-source materials are demoted to PRIVATE when only draft cases reference
    them (ADR: cases own no documents). Anon + non-privileged users must NOT see
    them; an authenticated caseworker/readonly/NGM-role principal may. UNLISTED
    is public-by-direct-IRI, so it is NOT gated here (see PUBLIC_VISIBILITIES).
    """
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated):
        return False
    # A superuser (or a Django-admin-session staff principal) may inspect drafts.
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True
    # Everyone else needs a read-capable role. ReadOnly (systemwide read) and the
    # NGM content role(s) are checked in a SINGLE group query — the bearer
    # authenticator syncs roles into Groups but never sets is_staff, so a
    # ReadOnly or bearer-only Caseworker principal is caught here, not by the
    # is_staff short-circuit above.
    return user.groups.filter(name__in=_NONPUBLIC_READ_GROUPS).exists()


def _with_admin_visibility(row) -> dict:
    """A COPY of a stored material's JSON-LD annotated with its cached
    ``visibility`` + caseworker ``visibility_policy``, for authed (caseworker/
    readonly) reads so the FE editor can render + drive the policy control. Public
    reads never see these fields; derived (court-case) docs have no stored row and
    are not annotated. We copy so the stored ``data`` dict is never mutated.
    """
    doc = dict(row.data)
    doc["jawafdehi:visibility"] = row.visibility
    doc["jawafdehi:visibilityPolicy"] = row.visibility_policy
    return doc


def _clean_policy(body):
    """Extract + validate an optional ``visibility_policy`` from a write body.

    Returns ``(policy_or_None, error_response_or_None)``: ``(None, None)`` when the
    key is absent (no change), ``(POLICY, None)`` when valid, or
    ``(None, Response(400))`` when present but not a known ``Policy``.
    """
    if not isinstance(body, dict) or "visibility_policy" not in body:
        return None, None
    policy = str(body.get("visibility_policy") or "").strip().upper()
    if policy not in Policy.values:
        return None, Response(
            {"detail": f"visibility_policy must be one of {sorted(Policy.values)}."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return policy, None


def _upsert_material(
    data,
    *,
    material_type: str | None,
    expected_iri: str | None = None,
    visibility_policy: str | None = None,
):
    """Validate + upsert a Material from a JSON-LD body. Returns (doc, status, error).

    On success: ``(doc, 200_or_201, None)``. On validation failure:
    ``(None, 400, {"detail": ...})``. ``material_type`` is inferred from the doc's
    ``additionalType``/``@type`` (via :func:`jsonld.infer_material_type`) when not
    supplied. When ``expected_iri`` is set (the PUT path) the body's @id must match
    the URL's IRI. ``visibility_policy`` (optional) is an explicit caseworker
    override; when omitted a NEW row is born at the source-derived default.
    """
    if not isinstance(data, dict) or "@id" not in data:
        return None, status.HTTP_400_BAD_REQUEST, {
            "detail": "Body must be a JSON-LD object with an '@id'."
        }
    if expected_iri is not None and data.get("@id") != expected_iri:
        return None, status.HTTP_400_BAD_REQUEST, {
            "detail": f"Body @id {data.get('@id')!r} must match the URL IRI {expected_iri!r}."
        }
    mtype = material_type or jsonld.infer_material_type(data)
    # 200-vs-201 is a report of whether a row already existed. Read it before the
    # write; the upsert itself is idempotent so a lost race only mislabels the
    # status, never the data. Pinned to ngm (where Material lives) to match the
    # write alias and avoid a replica read racing the primary.
    existed = Material.objects.using("ngm").filter(pk=data["@id"]).exists()
    try:
        # Delegate the write to the one upsert primitive (validation +
        # update_or_create by @id + created_at preservation + soft-delete revive
        # + post_save indexing all live there).
        material = upsert_single_source_material(
            data, material_type=mtype, visibility_policy=visibility_policy
        )
    except (ValueError, DjangoValidationError) as exc:
        detail = exc.message_dict if isinstance(exc, DjangoValidationError) else str(exc)
        return None, status.HTTP_400_BAD_REQUEST, {"detail": detail}
    # Settle the cached ``visibility`` from the (possibly new) policy so an anon
    # read reflects it immediately. The materials API is low-volume (not the bulk
    # importer, which relies on recompute_all / the case-side trigger), so a
    # per-write recompute is cheap. Best-effort: never fail a stored write on it.
    try:
        from .visibility import recompute_material_visibility

        recompute_material_visibility(material.iri)
    except Exception:  # noqa: BLE001 - the row is written; visibility can heal.
        logger.warning("visibility recompute failed for %s", material.iri, exc_info=True)
    code = status.HTTP_200_OK if existed else status.HTTP_201_CREATED
    return material.data, code, None


def _resolve_material(iri: str, *, include_nonpublic: bool = False) -> dict | None:
    """Return the JSON-LD doc for ``iri``: stored (live) row, else a derived
    court case. Soft-deleted rows (``is_deleted=True``) are treated as absent.

    Visibility gate (ADR: cases own no documents): by default only PUBLIC
    materials (LISTED + UNLISTED) resolve — a PRIVATE (draft-only) material is
    treated as absent (404) for the public. ``include_nonpublic=True`` (an authed
    caseworker/readonly/NGM principal) lifts the gate. Derived court-case
    materials have no stored row and are always public.
    """
    from .models import PUBLIC_VISIBILITIES

    try:
        row = Material.objects.get(pk=iri, is_deleted=False)
    except Material.DoesNotExist:
        # No stored row: fall back to the on-the-fly court-case derivation
        # (court cases are a public read plane in their own right).
        return _derive_court_case_jsonld(iri)

    if include_nonpublic:
        # Authed caseworker/readonly: surface the cached visibility + policy so the
        # FE editor can render + drive the control.
        return _with_admin_visibility(row)
    if row.visibility in PUBLIC_VISIBILITIES:
        return row.data
    # A PRIVATE (draft-only) stored row is treated as ABSENT for the public.
    # We must NOT fall through to _derive_court_case_jsonld here: doing so would
    # ignore the material's own visibility gate and hand an anon caller the
    # derived court-case document for an IRI the stored row marks non-public.
    return None


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


def _patch_visibility_policy(request, iri: str) -> Response:
    """``PATCH`` a material's caseworker ``visibility_policy`` + recompute.

    Body ``{"visibility_policy": "PUBLIC"|"CASE_GATED"|"PRIVATE"}``. NGM-role
    (Caseworker) gated. Sets the policy on the existing stored row, then settles
    the cached ``visibility`` via ``recompute_material_visibility`` (which also
    reconciles the search index through ``post_save``). Returns the updated doc
    annotated with ``jawafdehi:visibility``/``jawafdehi:visibilityPolicy`` (200),
    404 if no live stored row exists (derived court-case materials have no policy).
    """
    denied = _require_ngm_role(request)
    if denied is not None:
        return denied
    body = request.data if isinstance(request.data, dict) else {}
    policy, error = _clean_policy(body)
    if error is not None:
        return error
    if policy is None:
        return Response(
            {"detail": "Body must include a 'visibility_policy'."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    row = Material.objects.filter(pk=iri, is_deleted=False).first()
    if row is None:
        return Response(
            {"detail": "Material not found."}, status=status.HTTP_404_NOT_FOUND
        )
    if row.visibility_policy != policy:
        row.visibility_policy = policy
        row.save(update_fields=["visibility_policy", "updated_at"])
    from .visibility import recompute_material_visibility

    recompute_material_visibility(iri)
    row.refresh_from_db()
    return Response(
        _with_admin_visibility(row), status=status.HTTP_200_OK, content_type=LD_JSON
    )


@api_view(["GET", "PUT", "PATCH", "DELETE"])
@permission_classes([AllowAny])
def material_detail(request, source: str, ident: str):
    """``GET /api/materials/<source>/<ident>`` → the material JSON-LD doc.

    ``PUT`` replaces the material's stored JSON-LD (NGM-role gated). ``PATCH``
    sets the caseworker ``visibility_policy`` (NGM-role gated). ``DELETE``
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

    if request.method == "PATCH":
        return _patch_visibility_policy(request, iri)

    if request.method == "PUT":
        denied = _require_ngm_role(request)
        if denied is not None:
            return denied
        body = request.data if isinstance(request.data, dict) else {}
        material_type = body.get("material_type") if isinstance(body, dict) else None
        doc = body.get("material") if isinstance(body, dict) and "material" in body else body
        policy, error = _clean_policy(body)
        if error is not None:
            return error
        result, code, error = _upsert_material(
            doc, material_type=material_type, expected_iri=iri, visibility_policy=policy
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

    data = _resolve_material(iri, include_nonpublic=_can_see_nonpublic(request))
    if data is None:
        return Response({"detail": "Material not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(data, content_type=LD_JSON)


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
    default RAW), ``material_type`` (required only when creating a fresh material),
    ``skip_convert`` (optional; when truthy, suppress the automatic server-side
    ``material_convert`` re-OCR for a convertible role — for clients that supply
    their own extracted ``text``). NGM-role gated. Returns the material JSON-LD
    (201 created / 200 updated).
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
    if uploaded.size is not None and uploaded.size > _MAX_UPLOAD_BYTES:
        max_mb = _MAX_UPLOAD_BYTES // (1024 * 1024)
        return Response(
            {"detail": f"Uploaded file exceeds the {max_mb} MB limit."},
            status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
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

    # Capture provenance BEFORE storing (hashing rewinds the file pointer), then
    # stream to storage and attach a roled MediaObject carrying that provenance.
    # attach_media_object dedups on (role, sha256) so re-uploading the same bytes
    # replaces the link instead of appending a duplicate (content-hash idempotency).
    sha256 = provenance.content_sha256(uploaded)
    prov = provenance.build_provenance(
        sha256=sha256,
        fetch_method="upload",
        content_length=uploaded.size,
        source_url=(request.data.get("source_url") or None),
    )
    link = store_file_as_link(uploaded, role=role)
    encoding, _ = mimetypes.guess_type(uploaded.name)
    provenance.attach_media_object(
        doc,
        content_url=link["link"],
        role=link["role"],
        encoding_format=encoding or None,
        provenance=prov,
    )

    result, code, error = _upsert_material(
        doc, material_type=material_type, expected_iri=iri
    )
    if error is not None:
        return Response(error, status=code)

    # Feed full-text search: enqueue async OCR→text for a newly-attached source
    # document (data-plane FTS feed, docs/data-plane-design.md §5). Only for
    # OCR-able source roles — never for our own MARKDOWN output or SOURCE_PAGE
    # HTML. Idempotent (dedup on the IRI); best-effort so a queue hiccup never
    # fails the upload.
    #
    # ``skip_convert`` lets a client that ALREADY holds authoritative extracted
    # text (e.g. a sourcing pipeline that ran its own OCR/normalization and will
    # PUT ``text`` itself) suppress the server-side re-OCR that would otherwise
    # overwrite ``data["text"]`` — and re-incur the OCR cost. Truthy values:
    # "1"/"true"/"yes"/"on" (case-insensitive). Default off (unchanged behavior).
    skip_convert = str(request.data.get("skip_convert") or "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    if role in _CONVERTIBLE_ROLES and not skip_convert:
        try:
            from .conversion import enqueue_material_convert

            enqueue_material_convert(iri)
        except Exception:  # noqa: BLE001 — the file is stored; convert can re-run.
            logger.exception("material_convert enqueue failed for %s", iri)

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

    Visibility (ADR: cases own no documents): anon/non-privileged callers see
    only LISTED materials; an authed caseworker/readonly/NGM principal sees all
    live rows (so the admin table can manage in-review/draft evidence).
    """
    from .models import Visibility

    can_see_nonpublic = _can_see_nonpublic(request)
    qs = Material.objects.filter(is_deleted=False)
    if not can_see_nonpublic:
        qs = qs.filter(visibility=Visibility.LISTED)
    params = request.query_params
    if (source := params.get("source")):
        qs = qs.filter(source=source)
    if (material_type := params.get("material_type")):
        qs = qs.filter(material_type=material_type)

    paginator = PlatformCursorPagination()
    page = paginator.paginate_queryset(qs, request)
    # Authed callers get the cached visibility + policy per row (for the admin
    # table); anon callers get the raw JSON-LD unchanged.
    docs = [
        _with_admin_visibility(row) if can_see_nonpublic else row.data for row in page
    ]
    return paginator.get_paginated_response(docs)


@api_view(["GET", "POST", "PATCH", "DELETE"])
@permission_classes([AllowAny])
def material_by_iri(request):
    """``GET /api/materials/`` → paginated list of materials (``{results, next}``);
    ``GET /api/materials/?iri=<full-iri>`` → a single material JSON-LD doc.

    The ``iri`` form accepts either the canonical full IRI or a bare
    ``/material/<source>/<ident>`` path (resolved against ``iri_base()``).

    ``POST`` creates/upserts a material from a JSON-LD body (NGM-role gated); the
    body is either a bare JSON-LD doc (``@id`` present) or the
    ``{"material": {...}, "material_type": "..."}`` envelope (``material_type`` is
    inferred from the doc's ``additionalType``/``@type`` when absent). An optional
    top-level ``visibility_policy`` sets the caseworker policy on the write.
    ``PATCH /api/materials/?iri=<full-iri>`` sets just the ``visibility_policy``
    (NGM-role gated). ``DELETE /api/materials/?iri=<full-iri>`` soft-deletes the
    material (NGM-role gated, 204).
    """
    if request.method == "POST":
        denied = _require_ngm_role(request)
        if denied is not None:
            return denied
        body = request.data if isinstance(request.data, dict) else {}
        material_type = body.get("material_type") if isinstance(body, dict) else None
        doc = body.get("material") if isinstance(body, dict) and "material" in body else body
        policy, error = _clean_policy(body)
        if error is not None:
            return error
        result, code, error = _upsert_material(
            doc, material_type=material_type, visibility_policy=policy
        )
        if error is not None:
            return Response(error, status=code)
        return Response(result, status=code, content_type=LD_JSON)

    iri = (request.query_params.get("iri") or "").strip()

    if request.method == "PATCH":
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
        return _patch_visibility_policy(request, iri)

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
    data = _resolve_material(iri, include_nonpublic=_can_see_nonpublic(request))
    if data is None:
        return Response({"detail": "Material not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(data, content_type=LD_JSON)
