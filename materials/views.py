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

import hashlib
import logging
import mimetypes

import jsonpatch
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from django.utils.cache import patch_vary_headers
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
from jawafdehi_shared.jsonpatch_ops import normalize_patch_ops
from jawafdehi_shared.storage import store_file_as_link
from courts.permissions import NGM_ROLE_GROUPS, HasNgmRole

from . import jsonld
from . import provenance
from .models import Material, Policy
from .patch_validation import RESERVED_WRITE_KEYS, is_blocked_patch_path
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
    # Drop server-owned control/annotation keys so they never land in stored data
    # (a bare body's top-level visibility_policy; a round-tripped authed GET's
    # jawafdehi:visibility[Policy]). The policy itself arrives via the dedicated
    # `visibility_policy` argument, not the document.
    if RESERVED_WRITE_KEYS.intersection(data):
        data = {k: v for k, v in data.items() if k not in RESERVED_WRITE_KEYS}
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


def _material_read_response(request, iri: str) -> Response:
    """The shared GET tail for both material routes.

    One SELECT serves both the body and the ``ETag``: ``_resolve_material``
    already loaded (or proved absent) the stored row, so re-querying for the
    version token would double every read on this public, replica-served plane
    — and for a derived court-case IRI the second query could never return
    anything, because the absence is what put us on the derived path.

    ``Vary: Authorization`` because ONE URL serves two representations: anon gets
    ``row.data``, an authed caseworker gets it annotated with
    ``jawafdehi:visibility``/``jawafdehi:visibilityPolicy``. They share an entity
    tag (the row's version is the same either way), so without Vary a shared
    cache keyed on URL+ETag could hand the annotated body — and the material's
    visibility policy — to an anonymous caller.
    """
    data, row = _resolve_material(
        iri, include_nonpublic=_can_see_nonpublic(request), with_row=True
    )
    if data is None:
        resp = Response(
            {"detail": "Material not found."}, status=status.HTTP_404_NOT_FOUND
        )
        patch_vary_headers(resp, ("Authorization",))
        return resp
    resp = Response(data, content_type=LD_JSON)
    if row is not None:
        # The token a conditional PATCH is matched against, so a client can opt
        # into optimistic concurrency without a second round-trip.
        resp["ETag"] = _version_token(row)
    patch_vary_headers(resp, ("Authorization",))
    return resp


def _resolve_material(
    iri: str, *, include_nonpublic: bool = False, with_row: bool = False
):
    """Return the JSON-LD doc for ``iri``: stored (live) row, else a derived
    court case. Soft-deleted rows (``is_deleted=True``) are treated as absent.

    Visibility gate (ADR: cases own no documents): by default only PUBLIC
    materials (LISTED + UNLISTED) resolve — a PRIVATE (draft-only) material is
    treated as absent (404) for the public. ``include_nonpublic=True`` (an authed
    caseworker/readonly/NGM principal) lifts the gate. Derived court-case
    materials have no stored row and are always public.
    """
    from .models import PUBLIC_VISIBILITIES

    def _out(doc, row):
        # ``with_row`` returns the row alongside the doc so a caller needing the
        # material's version token doesn't have to SELECT it again. Callers that
        # only want the document keep the original single-value contract.
        return (doc, row) if with_row else doc

    try:
        row = Material.objects.get(pk=iri, is_deleted=False)
    except Material.DoesNotExist:
        # No stored row: fall back to the on-the-fly court-case derivation
        # (court cases are a public read plane in their own right). There is no
        # row, hence no version token — a derived material cannot be PATCHed.
        return _out(_derive_court_case_jsonld(iri), None)

    if include_nonpublic:
        # Authed caseworker/readonly: surface the cached visibility + policy so the
        # FE editor can render + drive the control.
        return _out(_with_admin_visibility(row), row)
    if row.visibility in PUBLIC_VISIBILITIES:
        return _out(row.data, row)
    # A PRIVATE (draft-only) stored row is treated as ABSENT for the public.
    # We must NOT fall through to _derive_court_case_jsonld here: doing so would
    # ignore the material's own visibility gate and hand an anon caller the
    # derived court-case document for an IRI the stored row marks non-public.
    return _out(None, None)


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


# ETag / optimistic-concurrency helper. A material's ``updated_at`` is a strong
# enough version token: every accepted write bumps ``auto_now``, so a stale token
# reliably signals the caller patched from an out-of-date copy. Hashed to an
# opaque quoted token so clients treat it as a cursor, not a timestamp to reason
# about (mirrors the case endpoint's contract).
def _version_token(row: Material) -> str:
    """Opaque, quoted ETag-style token derived from the material's updated_at."""
    basis = f"{row.pk}:{row.updated_at.isoformat() if row.updated_at else ''}"
    return f'"{hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]}"'


def _if_match_matches(request, row: Material) -> bool:
    """Whether the request's ``If-Match`` matches the material's current token.

    Absent header → True (the precondition is opt-in; existing clients and the
    moderation UI send none). Tolerates the ``W/`` weak-validator prefix and a
    bare unquoted token. ``*`` matches any existing row per RFC 7232 — a caller
    asserting only "it still exists".
    """
    raw = request.headers.get("If-Match", "").strip()
    if not raw:
        return True
    current = _version_token(row)
    for candidate in (t.strip() for t in raw.split(",")):
        if candidate == "*":
            return True
        norm = candidate[2:].strip() if candidate.startswith("W/") else candidate
        if norm == current or norm == current.strip('"'):
            return True
    return False


def _stored_etag(iri: str) -> str | None:
    """The current version token for a stored material, or None if there is none.

    Derived (court-case) materials have no stored row and therefore no version to
    hand out — a client cannot ``If-Match`` something it cannot PATCH either.

    Pinned to ``ngm`` (the PRIMARY) rather than routed: ``/api/materials/`` is not
    in ``config.middleware._PRIMARY_ONLY_PREFIXES``, so a safe-method read is
    replica-eligible. A version token is a WRITE precondition — it has to be
    minted from the same database ``_patch_material`` validates it against, or a
    lagging replica hands out a stale token and the caller gets a 412 loop on an
    edit nobody else touched.
    """
    row = (
        Material.objects.using("ngm")
        .filter(pk=iri, is_deleted=False)
        .only("iri", "updated_at")
        .first()
    )
    return _version_token(row) if row is not None else None


def _read_patch_body(request):
    """Split a PATCH body into ``(raw_patch_ops, policy_body)``.

    Two accepted spellings, both already in use on this platform:

      * a bare RFC-6902 array — what the case endpoint takes;
      * ``{"patch_ops": [...]}`` — what the NES entity endpoint takes, and the
        only spelling that can also carry ``visibility_policy`` in the same
        request.

    The pre-existing ``{"visibility_policy": ...}`` body is the second form with
    no ops, so the original contract is a strict subset of this one.
    """
    if isinstance(request.data, list):
        return request.data, {}
    body = request.data if isinstance(request.data, dict) else {}
    return body.get("patch_ops"), body


def _patch_material(request, iri: str) -> Response:
    """``PATCH`` a material: field-level JSON Patch and/or the visibility policy.

    NGM-role (Caseworker) gated. Body carries an RFC-6902 ``patch_ops`` list
    applied to the stored JSON-LD, a ``visibility_policy``, or both — at least
    one is required (a body that changes nothing stays a 400 rather than a 200 a
    caller would misread as success).

    The read-modify-write happens HERE, under ``select_for_update``, which is the
    reason this endpoint exists: the ``PUT`` path replaces ``data`` wholesale, so
    every client editing one key had to GET, merge and PUT with no way to detect
    that someone else wrote in between. Callers that want to detect it anyway can
    send ``If-Match`` with the token from a prior GET → 412 instead of a clobber.

    Returns the updated doc annotated with ``jawafdehi:visibility``/
    ``jawafdehi:visibilityPolicy`` (200) plus the new ``ETag``; 404 if no live
    stored row exists (derived court-case materials have no stored document).
    """
    denied = _require_ngm_role(request)
    if denied is not None:
        return denied

    raw_ops, body = _read_patch_body(request)
    policy, error = _clean_policy(body)
    if error is not None:
        return error
    if raw_ops is None and policy is None:
        return Response(
            {"detail": "Body must include 'patch_ops' and/or a 'visibility_policy'."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Validate the op list BEFORE touching the row: a blocked or malformed op
    # rejects the whole patch, so no partially-applied write can reach the DB.
    patch_ops = None
    if raw_ops is not None:
        try:
            patch_ops = normalize_patch_ops(raw_ops, is_blocked=is_blocked_patch_path)
        except ValueError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

    from .visibility import _referring_case_states, visibility_for_policy

    # ``ngm`` is where Material lives (config.db_router.ServiceDatabaseRouter);
    # naming it explicitly keeps the atomic block on the same connection the row
    # lock is taken on. NOTE: SQLite has ``has_select_for_update = False``, so
    # Django SILENTLY omits FOR UPDATE there — the lock is real on Postgres
    # (prod) but a no-op under the sqlite test gate. The concurrency tests below
    # therefore cover the ``If-Match`` contract, not the row lock itself.
    with transaction.atomic(using="ngm"):
        row = (
            Material.objects.using("ngm")
            .select_for_update()
            .filter(pk=iri, is_deleted=False)
            .first()
        )
        if row is None:
            return Response(
                {"detail": "Material not found."}, status=status.HTTP_404_NOT_FOUND
            )

        # Precondition check inside the lock — outside it, the row could change
        # between the check and the write and the guarantee would be theatre.
        if not _if_match_matches(request, row):
            resp = Response(
                {
                    "detail": (
                        "This material was modified since you read it. "
                        "Re-read it before patching."
                    )
                },
                status=status.HTTP_412_PRECONDITION_FAILED,
            )
            resp["ETag"] = _version_token(row)
            return resp

        updated_fields: list[str] = []

        if patch_ops is not None:
            try:
                patched = jsonpatch.apply_patch(row.data, patch_ops, in_place=False)
            except (
                jsonpatch.JsonPatchException,
                jsonpatch.JsonPointerException,
            ) as exc:
                return Response(
                    {"detail": f"Invalid JSON Patch document: {exc}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # A patch may not leave the stored document in a state the write plane
            # would have rejected (e.g. `remove /name`). Pin @id to the URL's IRI
            # as well — blocked paths already prevent repointing it, but this is
            # the durable check, not a restatement of the same one.
            try:
                jsonld.validate_material_jsonld(patched, iri=iri)
            except ValueError as exc:
                return Response(
                    {"detail": f"Patched material is invalid: {exc}"},
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            if patched != row.data:
                row.data = patched
                updated_fields.append("data")
                # This verb writes `data` directly instead of going through
                # `upsert_single_source_material`, so the model-layer invariant
                # has to be invoked explicitly or it silently stops applying on
                # PATCH alone. `Material.clean()` re-checks that the promoted
                # source/ident columns still agree with the doc's @id — cheap
                # insurance that any promoted column added later cannot drift on
                # this path while holding on every other one.
                try:
                    row.full_clean(validate_unique=False)
                except DjangoValidationError as exc:
                    return Response(
                        {"detail": f"Patched material is invalid: {exc}"},
                        status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    )

        if policy is not None:
            # Derive the new cached visibility from the new policy and persist BOTH
            # in one save — a synchronized write, so post_save fires exactly once
            # with the final state (the search-index reconcile never sees a stale
            # intermediate). Folding the doc change into that same save keeps the
            # indexer from seeing a half-applied PATCH too.
            new_visibility = visibility_for_policy(
                policy, lambda: _referring_case_states(iri)
            )
            if row.visibility_policy != policy or row.visibility != new_visibility:
                row.visibility_policy = policy
                row.visibility = new_visibility
                updated_fields += ["visibility_policy", "visibility"]

        if updated_fields:
            row.save(update_fields=updated_fields + ["updated_at"])

    resp = Response(
        _with_admin_visibility(row), status=status.HTTP_200_OK, content_type=LD_JSON
    )
    resp["ETag"] = _version_token(row)
    return resp


@api_view(["GET", "PUT", "PATCH", "DELETE"])
@permission_classes([AllowAny])
def material_detail(request, source: str, ident: str):
    """``GET /api/materials/<source>/<ident>`` → the material JSON-LD doc.

    ``PUT`` replaces the material's stored JSON-LD (NGM-role gated). ``PATCH``
    edits it field-by-field and/or sets the caseworker ``visibility_policy``
    (NGM-role gated). ``DELETE`` soft-deletes the stored material (NGM-role
    gated, 204). The GET path is byte-identical to before (stored row, else
    on-the-fly court-case fallback), plus an ``ETag`` when a stored row exists.

    ``PATCH`` body — an RFC-6902 JSON Patch list, a policy, or both::

        {"patch_ops": [{"op": "add",
                        "path": "/jawafdehi:caseNumber",
                        "value": "082-CR-0100"}],
         "visibility_policy": "PUBLIC"}

    A bare ``[{...}]`` array is accepted as the ops list too (the spelling the
    case endpoint uses). ``/@id``, ``/@context``, ``/@type``, ``/additionalType``
    and the visibility keys are not patchable (422) — see
    ``materials.patch_validation``. Send ``If-Match`` with the ``ETag`` from a
    prior GET to make the write conditional (412 if it changed meanwhile).
    """
    try:
        iri = build_material_iri(source, ident)
    except ValueError:
        return Response(
            {"detail": "Invalid material source/ident."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if request.method == "PATCH":
        return _patch_material(request, iri)

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

    return _material_read_response(request, iri)


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
    ``PATCH /api/materials/?iri=<full-iri>`` applies an RFC-6902 ``patch_ops``
    list to the stored JSON-LD and/or sets the ``visibility_policy`` (NGM-role
    gated) — same body and same guards as the ``<source>/<ident>`` route; see
    :func:`material_detail`. ``DELETE /api/materials/?iri=<full-iri>``
    soft-deletes the material (NGM-role gated, 204).
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
        # Authenticate BEFORE validating the iri query param (matches the sibling
        # DELETE branch + _require_ngm_role's contract: anon is 401, not a 400/422
        # input-validation disclosure).
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
        return _patch_material(request, iri)

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
    return _material_read_response(request, iri)
