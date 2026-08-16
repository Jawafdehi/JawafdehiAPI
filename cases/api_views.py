"""
API ViewSets for the Jawafdehi accountability platform.

See: .kiro/specs/accountability-platform-core/design.md
"""

import logging
import re
from html import escape
from urllib.parse import unquote
from xml.etree.ElementTree import Element, SubElement, tostring

import jsonpatch
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.contrib.auth import get_user_model
from django.db.models import Exists, F, OuterRef, Q
from django.http import Http404, HttpResponse, HttpResponsePermanentRedirect
from django.urls import reverse
from django.utils import timezone
from django.utils.cache import patch_vary_headers
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
)
from rest_framework import filters, mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, DjangoModelPermissions, IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from jawafdehi_shared.drf.auditlog import AuditlogActorMixin
from jawafdehi_shared.entities.ids import canonicalize_courtcase_iri
from jawafdehi_shared.identity import (
    JAWAFDEHI_USER_ID_HEADER,
    resolve_or_create_identity,
)
from jawafdehi_shared.storage import absolute_media_url

from .caseworker_serializers import (
    BLOCKED_PATH_PREFIXES,
    CaseCreateSerializer,
    CasePatchSerializer,
)
from .permissions import IsCaseAuthorPicker, IsFeedbackTriager
from .models import (
    AuthorProfile,
    Case,
    CaseEntityRelationship,
    CaseMaterialReference,
    CaseSlugHistory,
    CaseState,
    CaseStateChange,
    Feedback,
    FeedbackType,
    RelationshipOutcome,
    RelationshipType,
    StatisticsSnapshot,
)
from .rules.predicates import (
    can_change_case,
    can_transition_case_state,
    can_view_case,
    is_admin_or_moderator,
    is_readonly,
)
from .serializers import (
    AuthorCaseSummarySerializer,
    AuthorProfileDetailSerializer,
    CaseAuthorCandidateSerializer,
    CaseDetailSerializer,
    CaseSerializer,
    CaseStateChangeSerializer,
    FeedbackSerializer,
    FeedbackTriageSerializer,
)
from .services.statistics import (
    STATISTICS_SNAPSHOT_KEY,
    bootstrap_placeholder,
    refresh_statistics,
)

logger = logging.getLogger(__name__)

User = get_user_model()

# PATCH-writable fields that map directly to Case model columns (persisted via a
# bulk UPDATE in partial_update). Module-level because it is a constant: it was
# rebuilt as a local frozenset on every PATCH request.
#
# NOTE: "evidence" and "court_cases" are deliberately absent. Neither is a Case
# column (they are the CaseMaterialReference / CaseCourtCaseReference joins), so
# they must never be written via Case.objects.update(); they are persisted
# separately via _write_material_references / _sync_courtcase_references when a
# /evidence or /court_cases patch op is present.
_PATCH_SCALAR_FIELDS = frozenset(
    [
        "title",
        "short_description",
        "description",
        "thumbnail_url",
        "banner_url",
        "case_start_date",
        "case_end_date",
        "tags",
        "key_allegations",
        "timeline",
        "slug",
        "missing_details",
        "bigo",
        "weight",
        # Internal casework notes (Case.notes TextField). A scalar column, so it
        # persists via the bulk UPDATE like the other scalars — the missing entry
        # here is what silently dropped a patched note (BB-28).
        "notes",
        # Public notes (Case.public_notes TextField: attribution + edit dates) —
        # also a scalar column; same persist path, read publicly.
        "public_notes",
        # The structured byline's two scalar halves. ``authors`` is NOT here —
        # it is a join, written by _sync_author_credits like court_cases.
        "case_publish_date",
        "public_edit_history",
    ]
)


def _recompute_material_visibility(material_iris) -> None:
    """Schedule a visibility recompute for the given material IRIs (on commit).

    A material's visibility is the MAX over its referring cases' states (ADR:
    cases own no documents), so whenever a case's evidence set OR state changes,
    every affected material — including ones just REMOVED from the case — must be
    recomputed, else a draft/closed case could leave evidence stale-LISTED (a
    leak) or a published case's evidence stuck PRIVATE. Best-effort: the materials
    app is cross-DB, so a failure here must never break the case write.
    """
    iris = [iri for iri in dict.fromkeys(material_iris) if iri]
    if not iris:
        return

    def _run():
        try:
            from materials.visibility import recompute_material_visibility

            for iri in iris:
                recompute_material_visibility(iri)
        except Exception:  # noqa: BLE001 - visibility is best-effort, never fatal
            logger.warning(
                "material-visibility recompute failed for %d material(s)",
                len(iris),
                exc_info=True,
            )

    transaction.on_commit(_run)


# NOTE: the former Jawafdehi-scoped ``UnifiedSearchView`` (an in-process ORM
# search over cases/entities/documents) was REMOVED in the unified-search cutover
# (plan decision #5: OpenSearch is the one-way substrate, no in-process fallback).
# Platform search now lives in the ``search`` app at ``GET /api/search/`` (see
# ``search``), which queries all four OpenSearch indices.


class CasePagination(PageNumberPagination):
    """Page-number pagination that lets the client size the page.

    The global default (``PageNumberPagination`` with ``PAGE_SIZE=20`` and no
    ``page_size_query_param``) caps every list at 20 and ignores ``?page_size=``.
    The moderation queue (``?state=IN_REVIEW``) and the admin dashboard both
    need to fetch/​count more than 20 rows in one call, so this subclass honours
    ``?page_size=`` up to a bounded max. It stays a *page-number* paginator (not
    cursor) so the ``count`` field is preserved — the dashboard derives queue
    depth / draft counts from ``count`` with ``page_size=1``.
    """

    page_size = 20  # unchanged default so existing callers see no difference
    page_size_query_param = "page_size"
    max_page_size = 200


# ETag / optimistic-concurrency helper. A case's ``updated_at`` is a strong
# enough version token: any accepted PATCH bumps ``auto_now``, so a stale token
# reliably signals the caller edited from an out-of-date copy. We hash it to an
# opaque quoted token so clients treat it as a cursor, not a timestamp to reason
# about, and so a future switch to a real version column is invisible to them.
def _version_token(case) -> str:
    """Opaque, quoted ETag-style token derived from the case's updated_at.

    Returns e.g. ``"a1b2c3d4"``. Quotes make it a well-formed ETag value so it
    can be echoed in the ``ETag`` response header and matched against
    ``If-Match``.
    """
    import hashlib

    basis = f"{case.pk}:{case.updated_at.isoformat() if case.updated_at else ''}"
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
    return f'"{digest}"'


def _if_match_matches(request, case) -> bool:
    """Whether the request's ``If-Match`` header matches the case's current token.

    Tolerates the ``W/`` weak-validator prefix and a bare (unquoted) token so a
    slightly-off client still interoperates. ``*`` matches any existing row
    (per RFC 7232) — a caller asserting only "it still exists".
    """
    raw = request.headers.get("If-Match", "").strip()
    if not raw:
        return True  # no precondition supplied → not our concern here
    current = _version_token(case)
    for candidate in (t.strip() for t in raw.split(",")):
        if candidate == "*":
            return True
        # Normalize weak prefix and optional missing quotes before comparing.
        norm = candidate[2:].strip() if candidate.startswith("W/") else candidate
        if norm == current or norm == current.strip('"'):
            return True
    return False


@extend_schema_view(
    create=extend_schema(
        summary="Create a draft case",
        description="""
        Create a new case through the model-layer validation rules (`Case.validate()` / `Case.save()`).

        Authenticated users create cases in `DRAFT` state only. The request user is
        automatically added as a contributor on the new case.
        """,
        request=CaseCreateSerializer,
        responses={201: CaseSerializer},
        tags=["cases"],
    ),
    list=extend_schema(
        summary="List published cases",
        description="""
        Retrieve a paginated list of accountability cases.

        **Visibility rules:**
        - Unauthenticated requests: only PUBLISHED cases.
        - Content staff (Caseworker / superuser) and ReadOnly: all non-CLOSED
          cases (PUBLISHED + IN_REVIEW + DRAFT).
        - Other authenticated users (no role): only PUBLISHED cases.

        Results are ordered by creation date (newest first).

        **Filtering:**
        - `case_type`: Filter by case type (CORRUPTION)
        - `state`: Filter by workflow state (DRAFT / IN_REVIEW / PUBLISHED). Applied
          after visibility scoping, so callers only ever see states they may view
          (e.g. `?state=IN_REVIEW` is the moderation queue for casework roles).
        - `tags`: Filter cases containing a specific tag

        **Search:**
        - `search`: Full-text search across title, description, and key allegations

        **Pagination:**
        - Results are paginated with 20 items per page
        - Use `page` parameter to navigate pages
        """,
        parameters=[
            OpenApiParameter(
                name="case_type",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by case type",
                enum=["CORRUPTION"],
                required=False,
            ),
            OpenApiParameter(
                name="state",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter by workflow state (visibility-scoped)",
                enum=["DRAFT", "IN_REVIEW", "PUBLISHED"],
                required=False,
            ),
            OpenApiParameter(
                name="tags",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter cases containing this tag",
                required=False,
            ),
            OpenApiParameter(
                name="entity",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description=(
                    "Reverse lookup: published cases citing this NES entity by "
                    "its canonical @id IRI (e.g. "
                    "https://jawafdehi.org/entity/person/some-slug). "
                    "Accused/alleged citations are ordered first, then "
                    "reverse-chronologically."
                ),
                required=False,
            ),
            OpenApiParameter(
                name="courtcase",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description=(
                    "Reverse lookup: PUBLISHED cases citing this court case by "
                    "its canonical @id IRI (e.g. "
                    "https://jawafdehi.org/courtcase/kathmandudc/081-fn-12327). "
                    "Reverse-chronological. Unlike the other filters this one "
                    "is PUBLISHED-only for EVERY caller, including casework "
                    "roles — it powers a public court-record page. Fails "
                    "closed: a malformed or empty value returns an empty page, "
                    "never an unfiltered one. Composes with `entity` (both "
                    "narrow, and PUBLISHED-only still wins)."
                ),
                required=False,
            ),
            OpenApiParameter(
                name="search",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Search across title, description, and key allegations",
                required=False,
            ),
            OpenApiParameter(
                name="page",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description="Page number for pagination",
                required=False,
            ),
        ],
        tags=["cases"],
    ),
    retrieve=extend_schema(
        summary="Retrieve a case",
        description="""
        Retrieve detailed information about a specific case.

        The endpoint accepts either a numeric ID (deprecated) or a slug (preferred format: kebab-case).

        This endpoint includes complete case data (title, description, allegations,
        evidence, timeline) and any internal notes.

        **Access control:**
        - PUBLISHED cases: accessible to everyone (public, listed + searchable)
        - IN_REVIEW cases: UNLISTED but publicly retrievable by direct slug —
          accessible to everyone, just kept out of listings and search
        - DRAFT cases (casework): require a casework-viewing role
          (ReadOnly / Caseworker / Moderator / Admin); anonymous/public callers get 404
        - CLOSED cases: not accessible via this API

        Returns 404 if the case doesn't exist or if the user is not authorized to view it.
        """,
        parameters=[
            OpenApiParameter(
                name="id",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.PATH,
                description="Case identifier - either numeric ID (deprecated) or slug",
                required=True,
            ),
        ],
        tags=["cases"],
    ),
)
class CaseViewSet(AuditlogActorMixin, viewsets.ReadOnlyModelViewSet):
    """
    Public read-only API for Cases (with PATCH support for authenticated users).

    Provides:
    - Create endpoint: POST /api/cases/ (authenticated; write authorization in create)
    - List endpoint: GET /api/cases/
    - Retrieve endpoint: GET /api/cases/{id}/
    - Patch endpoint: PATCH /api/cases/{id}/ (authenticated; gated by can_change_case)

    Filtering:
    - case_type: Filter by case type
    - tags: Filter by tags

    Search:
    - Full-text search across title, description, key_allegations

    Read visibility is state-based. LISTING and SEARCH: unauthenticated/public
    callers see PUBLISHED only; casework roles (Admin / Moderator / Caseworker /
    ReadOnly) also see DRAFT + IN_REVIEW. RETRIEVE by slug is wider: PUBLISHED and
    IN_REVIEW are public (IN_REVIEW is "unlisted" — reachable by direct slug but
    absent from listings/search); DRAFT stays casework-only; CLOSED is never
    exposed via this API.
    """

    serializer_class = CaseSerializer
    lookup_field = "slug"
    # Client-sizable page-number pagination (preserves ``count`` for the
    # dashboard; honours ``?page_size=`` for the moderation queue).
    pagination_class = CasePagination
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    # ``state`` powers the moderation queue (GET /api/cases/?state=IN_REVIEW,
    # plan §G1). Filtering runs AFTER get_queryset()'s visibility scoping, so a
    # public caller filtering ?state=IN_REVIEW still gets nothing (the base
    # queryset is PUBLISHED-only) — visibility is preserved.
    filterset_fields = ["case_type", "state"]
    search_fields = ["title", "description", "key_allegations"]
    # Auth: inherit the OIDC-only DEFAULT_AUTHENTICATION_CLASSES (no per-view
    # pin). Unauthenticated reads still work because the actions use
    # get_permissions()/get_queryset() to gate visibility, not authentication.

    def get_permissions(self):
        # create requires the cases.add_case model permission (DjangoModelPermissions
        # maps POST->add_case) on top of authentication, so the org-wide ReadOnly
        # role (view-only perms) and plain authenticated users without add_case
        # cannot create cases. partial_update stays IsAuthenticated here; its
        # authorization is the can_change_case check inside partial_update().
        if self.action == "create":
            return [IsAuthenticated(), DjangoModelPermissions()]
        if self.action == "partial_update":
            return [IsAuthenticated()]
        if self.action == "destroy":
            # DjangoModelPermissions maps DELETE -> cases.delete_case, keeping the
            # org-wide ReadOnly role and plain authenticated users (no delete_case)
            # out of the soft-delete path.
            return [IsAuthenticated(), DjangoModelPermissions()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == "create":
            return CaseCreateSerializer
        if self.action == "retrieve":
            return CaseDetailSerializer
        return CaseSerializer

    def get_queryset(self):
        """
        Return cases filtered by state.

        List endpoint: PUBLISHED cases only for anonymous/public; casework roles
        (Admin/Moderator/Caseworker/ReadOnly) also see IN_REVIEW + DRAFT.
        Retrieve endpoint:
          - Unauthenticated/public: PUBLISHED + IN_REVIEW (IN_REVIEW is unlisted
            but reachable by direct slug); DRAFT → 404
          - Casework roles: PUBLISHED, IN_REVIEW, and DRAFT (authz check in retrieve)
          - CLOSED cases are never exposed via this API
        Partial update endpoint: all cases except CLOSED (authorization check happens in partial_update).
        """
        if self.action == "create":
            # DjangoModelPermissions calls get_queryset() only to derive the model
            # for the add_case check; return an empty queryset (still carries
            # .model) so the list/tag-filtering path below does not run on POST.
            return Case.objects.none()

        if self.action in ("partial_update", "destroy"):
            # PATCH / DELETE endpoints: address any non-CLOSED case; the
            # authorization check happens in the action method (partial_update /
            # destroy). CLOSED cases are already "deleted" and not addressable.
            return Case.objects.exclude(state=CaseState.CLOSED)

        if self.action == "retrieve":
            # Retrieve-by-slug is wider than listing: IN_REVIEW is "unlisted" —
            # publicly reachable by direct slug, just absent from listings/search.
            # DRAFT stays casework-only; the per-object gate is in retrieve().
            if self.request.user and self.request.user.is_authenticated:
                # Exclude CLOSED cases from the API; the retrieve() gate enforces
                # casework-role for DRAFT.
                queryset = Case.objects.exclude(state=CaseState.CLOSED)
            else:
                # Unauthenticated: PUBLISHED + IN_REVIEW (unlisted-by-slug);
                # DRAFT and CLOSED stay hidden.
                queryset = Case.objects.filter(
                    state__in=[CaseState.PUBLISHED, CaseState.IN_REVIEW]
                )
        else:
            # List endpoint: visibility depends on authentication/role.
            # - Unauthenticated: PUBLISHED only
            # - Content staff (Caseworker/superuser) + ReadOnly: all non-CLOSED
            # - Other authenticated (no role): PUBLISHED only
            #   (v3: object-level case assignment is retired, so there is no
            #   "cases I'm assigned to" widening for role-less users.)
            if not (self.request.user and self.request.user.is_authenticated):
                queryset = Case.objects.filter(state=CaseState.PUBLISHED)
            elif is_admin_or_moderator(self.request.user) or is_readonly(
                self.request.user
            ):
                queryset = Case.objects.exclude(state=CaseState.CLOSED)
            else:
                queryset = Case.objects.filter(state=CaseState.PUBLISHED)

        # Apply tag filtering if provided
        tags_param = self.request.query_params.get("tags", None)
        if tags_param:
            # Filter cases that contain the specified tag
            # For SQLite, we need to filter in Python since it doesn't support JSON contains
            # For PostgreSQL, we can use the contains lookup
            if connection.vendor == "postgresql":
                queryset = queryset.filter(tags__contains=[tags_param])
            else:
                # For SQLite, filter by checking if tag is in the list
                # Get all case IDs that have the tag
                case_ids_with_tag = [
                    case.id
                    for case in queryset
                    if case.tags and tags_param in case.tags
                ]
                queryset = queryset.filter(id__in=case_ids_with_tag)

        queryset = queryset.prefetch_related(
            "entity_relationships",
            "courtcase_references",
            # ``CaseSerializer.get_evidence`` iterates ``material_references``;
            # prefetch it so a list page doesn't fire one query per card (N+1).
            "material_references",
            # Same for ``get_authors``; through to the profile, which the byline
            # resolves every name/photo/description from.
            "author_credits__user__author_profile",
        )

        # Reverse lookup: cases citing a specific NGM court case by its canonical
        # ``@id`` IRI (``CaseCourtCaseReference.courtcase_iri``), powering the
        # "Related Jawafdehi cases" section on a court case's own page. Flat and
        # reverse-chronological — ``CaseCourtCaseReference`` has no
        # ``relationship_type``, so there is no accused/alleged tier to float
        # here the way the ``entity`` branch below does.
        #
        # PUBLISHED-only for EVERY caller, deliberately NOT inheriting the
        # role-scoped queryset the ``entity`` branch uses: a court-case page is a
        # public archive record, not a casework surface. Caseworkers work on the
        # Jawafdehi case itself, so there is no reason for a DRAFT/IN_REVIEW case
        # to ever be named on one.
        #
        # This NARROWS ``queryset`` rather than returning, so it composes with
        # the ``entity`` branch below instead of being silently dropped when both
        # params are present. Applying it first also means its PUBLISHED-only
        # rule — the stricter of the two — survives the combination: a request
        # carrying ``courtcase`` can never widen past PUBLISHED via ``entity``.
        #
        # Presence, not truthiness: ``?courtcase=`` is malformed input, not an
        # absent filter, and must fail CLOSED to an empty page. A falsey check
        # would skip this branch and hand back the whole unfiltered case list —
        # exactly the failure mode this filter exists to prevent.
        courtcase_param = self.request.query_params.get("courtcase")
        if self.action == "list" and courtcase_param is not None:
            # Stored IRIs are canonical (``build_courtcase_iri`` lowercases court
            # + case number), so normalize the param first — otherwise a caller
            # echoing the court's own casing ("/KathmanduDC/081-FN-12327")
            # silently matches nothing. ``canonicalize_courtcase_iri`` alone is
            # not enough: its regex is lowercase-only, so it REJECTS mixed case
            # rather than folding it. Lowercasing the whole IRI first is safe
            # here — canonicalization discards the host and re-emits on the
            # canonical authority regardless.
            try:
                courtcase_iri = canonicalize_courtcase_iri(courtcase_param.lower())
            except ValueError:
                # Not a court-case IRI at all (including "") — nothing cites it.
                return queryset.none()
            # No ``.distinct()``: the ``unique_case_courtcase_reference``
            # constraint is on (case, courtcase_iri), so this join matches at
            # most one reference row per case and cannot duplicate a ``Case``.
            # The ``entity`` branch below DOES need it — its constraint includes
            # ``relationship_type``, so one case can hold several rows for the
            # same ``nes_id``.
            queryset = queryset.filter(
                state=CaseState.PUBLISHED,
                courtcase_references__courtcase_iri=courtcase_iri,
            )

        # Reverse lookup: cases citing a specific NES entity by its canonical
        # ``@id`` IRI (``CaseEntityRelationship.nes_id``), powering the "Related
        # cases" section on an entity's record page. List action only — retrieve
        # addresses a single case by slug. Visibility scoping above still holds,
        # so an anonymous caller only sees PUBLISHED citations. ``accused`` /
        # ``alleged`` cases float to the top; reverse-chron (``-created_at``)
        # within each tier.
        entity_param = self.request.query_params.get("entity")
        if self.action == "list" and entity_param:
            accused_first = Exists(
                CaseEntityRelationship.objects.filter(
                    case=OuterRef("pk"),
                    nes_id=entity_param,
                    relationship_type__in=[
                        RelationshipType.ACCUSED,
                        RelationshipType.ALLEGED,
                    ],
                )
            )
            return (
                queryset.filter(entity_relationships__nes_id=entity_param)
                .distinct()
                .annotate(_accused_first=accused_first)
                .order_by("-_accused_first", "-created_at")
            )

        return queryset.order_by("-created_at")

    # This list is ROLE-SCOPED: an anonymous caller gets a PUBLISHED-only page,
    # but the SAME URL returns a wider DRAFT/IN_REVIEW-inclusive page to an
    # authenticated caseworker. A shared/CDN cache keys by URL and cannot vary
    # on auth (Cloudflare honours no ``Vary`` other than Accept-Encoding), so a
    # cached anon snapshot would be handed to a signed-in caseworker and
    # silently hide their in-review cases. So the anon response is kept OUT of
    # shared caches (``private``) and only the browser may hold it briefly
    # (``max-age`` → an immediate reload is free). Deliberately NOT
    # ``public``/``s-maxage``: unlike StatisticsView (identical for every
    # caller, safe to edge-cache), this endpoint must never enter the edge
    # cache. The batched entity resolution + ``material_references`` prefetch
    # keep an uncached hit cheap, so forgoing the edge cache costs little.
    LIST_CACHE_CONTROL = "private, max-age=60"

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        if not (request.user and request.user.is_authenticated):
            response["Cache-Control"] = self.LIST_CACHE_CONTROL
            # ``private`` already keeps this out of shared/CDN caches; Vary on
            # the auth-bearing headers so the browser's OWN cache can't reuse
            # this anon entry for a request issued after the user signs in
            # (OIDC bearer ``Authorization`` or session ``Cookie``) — that one
            # must fetch the role-scoped list fresh.
            patch_vary_headers(response, ["Cookie", "Authorization"])
        else:
            # Role-scoped list (may include DRAFT/IN_REVIEW cases the public
            # must never see). Set an explicit ``no-store`` on every
            # authenticated path so a "Cache Everything" CDN rule can't store
            # this response and later serve it to an anonymous visitor. Mirror
            # StatisticsView, which sets a Cache-Control header on every branch.
            response["Cache-Control"] = "private, no-store"
            patch_vary_headers(response, ["Cookie", "Authorization"])
        return response

    # Case model fields that CaseCreateSerializer may set directly on the row.
    # ``court_cases`` is a settable property (canonical IRIs synced to the
    # CaseCourtCaseReference join on save), so it stays in this set. Non-model
    # serializer keys (alleged_entities / related_entities / evidence) are
    # handled separately as binds/joins below.
    _CREATE_MODEL_FIELDS = frozenset(
        [
            "case_type",
            "state",
            "title",
            "short_description",
            "description",
            "thumbnail_url",
            "banner_url",
            "case_start_date",
            "case_end_date",
            "tags",
            "key_allegations",
            "timeline",
            "notes",
            "public_notes",
            "case_publish_date",
            "public_edit_history",
            "slug",
            "court_cases",
            "missing_details",
            "bigo",
            "weight",
        ]
    )

    def create(self, request, *args, **kwargs):
        """
        POST /api/cases/

        Create a new case through the model-layer validation rules
        (``Case.validate()`` / ``Case.save()``), which are the single source of
        truth. Enforces DRAFT-on-create and the required-field rules that were
        previously re-invoked via ``CaseAdminForm``.
        """
        # Validate that request body is a JSON object (dict), not array or scalar
        if not isinstance(request.data, dict):
            return Response(
                {"detail": "Request body must be a JSON object."},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        allowed_fields = set(CaseCreateSerializer().fields.keys())
        unexpected_fields = sorted(set(request.data.keys()) - allowed_fields)
        if unexpected_fields:
            return Response(
                {field: ["This field is not allowed."] for field in unexpected_fields},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        serializer = CaseCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                serializer.errors, status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

        validated = serializer.validated_data

        # New cases must be DRAFT. This rule lived only in CaseAdminForm.clean()
        # (admin.py:271) — not at the model layer — so it is ported here to keep
        # create() lenient (DRAFT skips the allegation/description/entity gates)
        # while still refusing a client-supplied non-DRAFT create.
        if validated.get("state", CaseState.DRAFT) != CaseState.DRAFT:
            return Response(
                {
                    "state": [
                        "New cases must be created in DRAFT state. "
                        f"Cannot create a new case with state {validated.get('state')}."
                    ]
                },
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )

        # Build the Case from the validated scalar fields; force DRAFT.
        model_kwargs = {
            field: validated[field]
            for field in self._CREATE_MODEL_FIELDS
            if field in validated
        }
        model_kwargs["state"] = CaseState.DRAFT
        case = Case(**model_kwargs)

        # Model-layer validation is the single source of truth (title-required,
        # slug format, state-based required fields). ``validate()`` runs the
        # state rules and auto-generates the slug; ``save()`` re-checks the title
        # and slug immutability. Both raise ValidationError -> 422 field errors.
        # (Slug FORMAT was already enforced by the serializer's validate_slug
        # validator; blank slug is auto-generated, matching admin/save semantics.)
        try:
            case.validate()
            with transaction.atomic():
                case.save()

                # Create entity binds (NES ids) for alleged/related entities
                for nes_id in validated.get("alleged_entities", []):
                    CaseEntityRelationship.objects.get_or_create(
                        case=case,
                        nes_id=nes_id,
                        relationship_type=RelationshipType.ACCUSED,
                    )
                for nes_id in validated.get("related_entities", []):
                    CaseEntityRelationship.objects.get_or_create(
                        case=case,
                        nes_id=nes_id,
                        relationship_type=RelationshipType.RELATED,
                    )

                # Create evidence binds (NGM material ids) — the
                # CaseMaterialReference join. Ordinal preserves submitted order
                # (ADR: cases own no docs).
                self._write_material_references(case, validated.get("evidence", []))

                # Credited authors — the CaseAuthor join (an ordered list of
                # account ids). Only when the payload carried the key: passing []
                # unconditionally would be a write intent, and
                # _sync_author_credits treats that as "clear".
                if "authors" in validated:
                    case._sync_author_credits(validated["authors"])
        except ValidationError as exc:
            detail = getattr(exc, "message_dict", None) or {"detail": exc.messages}
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Newly-created evidence may reference materials whose visibility now
        # depends on this case's state — recompute (best-effort, on_commit).
        _recompute_material_visibility(
            case.material_references.values_list("material_iri", flat=True)
        )

        return Response(CaseSerializer(case).data, status=status.HTTP_201_CREATED)

    @staticmethod
    def _write_material_references(case, evidence_items):
        """Replace a case's CaseMaterialReference rows from validated evidence.

        ``evidence_items`` is a list of ``{material_iri, additional_details}``
        (order = display order). Existing rows are deleted and recreated so the
        set + ordering match the submitted evidence exactly (mirrors the
        entity-relationship rewrite).
        """
        case.material_references.all().delete()
        for ordinal, item in enumerate(evidence_items):
            CaseMaterialReference.objects.create(
                case=case,
                material_iri=item["material_iri"],
                additional_details=item.get("additional_details") or "",
                ordinal=ordinal,
            )

    def _slug_history_redirect(self, request, requested_slug):
        """Resolve a retired slug to a 301 redirect, or ``None`` for a true 404.

        Consulted only when :meth:`get_object` misses (no live case owns the
        slug), so a live slug is never overridden — the live case always wins
        (BB-38). Returns an ``HttpResponsePermanentRedirect`` to the case's
        canonical ``/api/cases/<current-slug>/`` (query string preserved) when
        ``requested_slug`` is a former slug of a case the requester may see;
        otherwise ``None`` (the caller re-raises the 404).

        Visibility mirrors :meth:`retrieve` exactly, so a retired slug never
        leaks a non-public case's existence: CLOSED is never exposed; DRAFT
        (casework) redirects only for a casework-viewing role; PUBLISHED and
        IN_REVIEW (unlisted-but-public-by-slug) redirect for anonymous callers.
        """
        if not requested_slug:
            return None

        history = (
            CaseSlugHistory.objects.filter(slug=requested_slug)
            .select_related("case")
            .first()
        )
        if history is None:
            return None

        case = history.case
        # CLOSED cases are never exposed via this API — don't confirm existence.
        if case.state == CaseState.CLOSED:
            return None
        # DRAFT is not public: only redirect for a casework-viewing role, else
        # 404 (same boundary retrieve() enforces for a live case). IN_REVIEW is
        # unlisted-but-public-by-slug, so its retired slugs redirect for anyone.
        if case.state == CaseState.DRAFT:
            user = request.user
            if not (
                user and user.is_authenticated and can_view_case(user, case)
            ):
                return None
        # Defensive: a self-redirect would loop. A live slug can't reach here
        # (get_object would have resolved it), but guard against a stale row.
        if case.slug == requested_slug:
            return None

        location = request.build_absolute_uri(
            reverse("case-detail", kwargs={"slug": case.slug})
        )
        query_string = request.META.get("QUERY_STRING", "")
        if query_string:
            location = f"{location}?{query_string}"
        return HttpResponsePermanentRedirect(location)

    def retrieve(self, request, *args, **kwargs):
        """
        GET /api/cases/{id}/

        Retrieve a case with state-based access control:
        - PUBLISHED cases: accessible to everyone (public, listed + searchable)
        - IN_REVIEW cases: UNLISTED but public by direct slug — accessible to
          everyone, just absent from listings/search
        - DRAFT cases (casework): require a casework-viewing role
          (readonly/caseworker/moderator/admin); public callers get 404
        - CLOSED cases: not accessible via public API (returns 404)

        When the slug matches no live case, fall back to CaseSlugHistory: a
        retired slug 301-redirects to the case's canonical URL (BB-38); a
        genuinely unknown slug stays a 404.
        """
        try:
            case = self.get_object()
        except Http404:
            redirect = self._slug_history_redirect(
                request, self.kwargs.get(self.lookup_field)
            )
            if redirect is not None:
                return redirect
            raise

        # DRAFT is the only non-public retrieve state: it requires a casework
        # role. IN_REVIEW is "unlisted" — public by direct slug (kept out of
        # listings/search via get_queryset + the search indexer) — and PUBLISHED
        # is public, so neither is gated here. CLOSED never reaches this method
        # (excluded from every retrieve queryset).
        if case.state == CaseState.DRAFT:
            # A DRAFT is casework-only: anon or a role-less user gets 404. This
            # mirrors history()'s gate; get_queryset already keeps DRAFT out of
            # the anon retrieve queryset, so this is the defensive object-level
            # check (can_view_case is False for AnonymousUser).
            if not request.user.is_authenticated or not can_view_case(
                request.user, case
            ):
                return Response(
                    {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
                )

        # Case is accessible - return serialized data. Carry the optimistic-
        # concurrency token so an editor can echo it back as ``If-Match`` on the
        # next PATCH.
        serializer = self.get_serializer(case)
        response = Response(serializer.data)
        response["ETag"] = _version_token(case)
        return response

    @extend_schema(
        summary="Case workflow history",
        description=(
            "Append-only log of a case's state transitions (who moved it, when, "
            "to what state, and any reason). Casework-role only for non-published "
            "cases — same visibility boundary as retrieve()."
        ),
        responses={200: CaseStateChangeSerializer(many=True)},
        tags=["cases"],
    )
    @action(detail=True, methods=["get"], url_path="history")
    def history(self, request, *args, **kwargs):
        """GET /api/cases/{slug}/history/

        The case author's feedback loop reads this to show "your submission was
        sent back to draft by <moderator>: <reason>". Reuses the exact same
        visibility gate as retrieve() (casework states are not public), so a
        history request can never leak a draft/in-review case's existence.
        """
        case = self.get_object()

        # The history carries internal casework data (moderator names + return
        # reasons) for EVERY state — including PUBLISHED — so it is gated
        # unconditionally, unlike retrieve() (which exposes a published case's
        # content to the public). Access = a casework-viewing role
        # (content staff or ReadOnly). Anyone else gets 404 so the endpoint
        # never confirms a case's existence to an outsider. (v3: the old
        # per-object contributor fallback is retired with Case.contributors.)
        if not request.user.is_authenticated or not can_view_case(request.user, case):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        # select_related("actor") so the serializer's actor_name lookup per row
        # doesn't fan out into N+1 queries.
        changes = case.state_changes.select_related("actor")  # newest first (Meta)
        page = self.paginate_queryset(changes)
        serializer = CaseStateChangeSerializer(
            page if page is not None else changes, many=True
        )
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    # --- partial_update helpers -------------------------------------------
    #
    # Each `_reject_*` returns an error Response to send, or None to continue.
    # They are split out of partial_update purely to make that ~400-line method
    # readable (it was radon F/50); every gate keeps its original order, status
    # code and message, because the PATCH contract is what 100+ tests pin.

    def _log_non_array_body(self, request, case):
        """Tripwire for a PATCH body that is not an RFC-6902 array.

        A valid client (the SPA) always sends a bare array here, so a non-list
        body is anomalous. An intermittent empty/object body was seen in prod
        publish traffic but could not be reproduced from the SPA — capture enough
        to identify the culprit if it recurs. The body is not a valid patch (so it
        carries no case content worth redacting); still truncate + type-tag it
        defensively.
        """
        body_repr = repr(request.data)
        if len(body_repr) > 500:
            body_repr = body_repr[:500] + "…"
        logger.warning(
            "cases.partial_update rejected a non-array PATCH body: "
            "type=%s content_type=%r content_length=%s user=%s case=%s ua=%r body=%s",
            type(request.data).__name__,
            request.content_type,
            request.META.get("CONTENT_LENGTH"),
            getattr(request.user, "username", None) or "anon",
            case.slug,
            request.META.get("HTTP_USER_AGENT", "")[:200],
            body_repr,
        )

    def _reject_blocked_paths(self, patch_ops, case):
        """Reject ops targeting a blocked path, before the patch is applied."""
        for op in patch_ops:
            if not isinstance(op, dict):
                return Response(
                    {"detail": "Each patch operation must be a JSON object."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            path = op.get("path", "")

            if path == "/state" and op.get("op") != "replace":
                return Response(
                    {
                        "detail": "State transition must use a 'replace' operation on '/state'."
                    },
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

            # Check if slug is being modified when case is not in DRAFT state
            if (
                path == "/slug" or path.startswith("/slug/")
            ) and case.state != CaseState.DRAFT:
                return Response(
                    {
                        "detail": f"Patching path '{path}' is not allowed. Slug can only be modified when case is in DRAFT state."
                    },
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )

            for blocked in BLOCKED_PATH_PREFIXES:
                if path == blocked or path.startswith(blocked + "/"):
                    return Response(
                        {"detail": f"Patching path '{path}' is not allowed."},
                        status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    )
        return None

    @staticmethod
    def _touches(patch_ops, path):
        """True when any op targets ``path`` or a child of it.

        Gates each join rewrite (entities / evidence / court-case refs) to ops
        that actually target its path: _build_snapshot always carries these keys,
        so they are always present in validated after apply_patch, and writing
        unconditionally would wipe the join on every scalar PATCH. isinstance
        guard on path: a non-string (malformed client op) must not AttributeError
        into a 500.
        """
        return any(
            isinstance(op, dict)
            and isinstance(op.get("path"), str)
            and (op["path"] == path or op["path"].startswith(path + "/"))
            for op in patch_ops
        )

    def _rewrite_entity_binds(self, case, entities):
        """Replace the whole entity-bind list. Returns a 422 Response or None.

        Preserve an accused bind's verdict across the whole-list delete/recreate
        when the client didn't send one, so an outcome-unaware client/script can't
        silently reset verdicts to 'charged'. Keyed by
        (nes_id, relationship_type) — the bind identity — so a re-sent accused
        bind keeps its verdict; a new accused bind falls back to 'charged', and
        non-accused roles carry no verdict at all.
        """
        prior_outcomes = {
            (rel.nes_id, rel.relationship_type): rel.outcome
            for rel in case.entity_relationships.all()
        }
        case.entity_relationships.all().delete()
        # Two payload entries with the same (nes_id, relationship_type) pass
        # serializer validation but collide on the
        # ``unique_case_entity_relationship_type`` DB constraint at .create()
        # (IntegrityError -> 500). Detect the dup here and return a field-keyed
        # 422 instead. set_rollback + return is the method's established in-atomic
        # 422 pattern (see the state transition block): a raised DRF
        # ValidationError would map to 400, and this project has no custom
        # exception handler.
        seen_binds: set[tuple[str, str]] = set()
        for item in entities:
            rtype = item["relationship_type"]
            key = (item["nes_id"], rtype)
            if key in seen_binds:
                transaction.set_rollback(True)
                return Response(
                    {
                        "entities": [
                            f"Duplicate entity bind: '{item['nes_id']}' "
                            f"as '{rtype}' appears more than once."
                        ]
                    },
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
            seen_binds.add(key)
            if rtype == RelationshipType.ACCUSED:
                # Distinguish an omitted "outcome" key from an explicit null.
                # When the client SENDS ``outcome`` (even null), honor it: a null
                # accused verdict is normalized back to 'charged' by the model
                # save(), so a client can reset a verdict to the default. Only
                # when the key is entirely OMITTED do we preserve the accused
                # bind's prior verdict across the whole-list replace; a brand-new
                # bind with no prior verdict falls back to 'charged'.
                if "outcome" in item:
                    outcome = item["outcome"]
                else:
                    outcome = prior_outcomes.get(key) or RelationshipOutcome.CHARGED
            else:
                # A verdict is meaningful only for ACCUSED; every other role stays
                # NULL (rejected earlier by the serializer, enforced by the model
                # save() + CHECK constraint).
                outcome = None
            CaseEntityRelationship.objects.create(
                case=case,
                nes_id=item["nes_id"],
                relationship_type=rtype,
                outcome=outcome,
                notes=item.get("notes") or "",
            )
        return None

    def _reject_before_patch(self, request, case):
        """Every pre-patch gate: permission, If-Match, body shape, blocked paths.

        Returns ``(response, patch_ops)``: a Response to send and ``None``, or
        ``None`` and the parsed ops to proceed with.

        Touching ``request.data`` is deliberately deferred until AFTER the
        permission and If-Match gates. DRF parses the body lazily on first
        access and raises ``ParseError`` (-> 400) on malformed JSON, so reading
        it any earlier turns an unauthorized or stale-token request into a 400 —
        leaking "your body was unparseable" to a caller that should only ever
        learn 403/412, and losing the 412's ETag reconciliation header.
        """
        if not can_change_case(request.user, case):
            return (
                Response(
                    {"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN
                ),
                None,
            )

        # Optimistic concurrency (opt-in). When the client sends ``If-Match`` with
        # the token it received on load, reject the write if the case has changed
        # since (last-write-wins would otherwise silently clobber a concurrent
        # edit — the whole-list replaces on entities/evidence make this costly).
        # Absent the header, behaviour is unchanged (backward compatible with
        # existing clients and scripts). 412 Precondition Failed is the RFC 7232
        # status; the response carries the current token so the client can
        # reconcile.
        if not _if_match_matches(request, case):
            resp = Response(
                {
                    "detail": (
                        "This case was modified since you opened it. "
                        "Reload to get the latest version before saving."
                    )
                },
                status=status.HTTP_412_PRECONDITION_FAILED,
            )
            resp["ETag"] = _version_token(case)
            return resp, None

        # First touch of the body — see the docstring on why it happens here.
        patch_ops = request.data
        if not isinstance(patch_ops, list):
            self._log_non_array_body(request, case)
            return (
                Response(
                    {
                        "detail": "Request body must be a JSON array of patch operations."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                ),
                None,
            )

        blocked = self._reject_blocked_paths(patch_ops, case)
        if blocked is not None:
            return blocked, None
        return None, patch_ops

    def _after_commit(self, case, affected_material_iris, *, evidence_touched, state_changed):
        """Post-commit side effects: re-index, then recompute material visibility.

        Scalar edits go through queryset .update() and entity-relationship edits
        through bulk delete/create — neither fires post_save, so the live
        search-index signal never runs. Re-index explicitly (best-effort,
        on_commit) so a PUBLISHED case's content/slug/relationships stay fresh in
        the index; a non-PUBLISHED case is evicted by the same call.
        """
        from .search_index import index as _index_case

        transaction.on_commit(lambda: _index_case(case))

        # A state transition OR an evidence-set change alters the visibility of the
        # referenced materials (visibility = MAX over referring case states).
        # Recompute the union of currently-referenced + just-removed materials so a
        # demoted case can't leave stale-LISTED evidence behind (ADR draft-leak
        # guard). Skipped only on pure scalar/entity PATCHes.
        if evidence_touched or state_changed:
            affected_material_iris.update(
                case.material_references.values_list("material_iri", flat=True)
            )
            _recompute_material_visibility(affected_material_iris)

    def _write_joins(self, case, validated, touched):
        """Rewrite the entity / evidence / court-case / author joins the patch touched.

        Returns ``(error_response, affected_material_iris)``. Each join is written
        only when an op actually targeted its path — writing unconditionally would
        wipe the join on every scalar PATCH.
        """
        entities_touched, evidence_touched, court_cases_touched, authors_touched = (
            touched
        )

        if entities_touched:
            dup_response = self._rewrite_entity_binds(case, validated["entities"])
            if dup_response is not None:
                return dup_response, set()

        # Capture the pre-rewrite IRIs so removed materials are recomputed too (a
        # material dropped from a published case must be re-evaluated, else it
        # stays LISTED via a stale referrer).
        affected_material_iris: set[str] = set()
        if evidence_touched:
            affected_material_iris.update(
                case.material_references.values_list("material_iri", flat=True)
            )
            self._write_material_references(case, validated.get("evidence", []))
            affected_material_iris.update(
                case.material_references.values_list("material_iri", flat=True)
            )

        # The model's sync is THE single court-case join writer (no-op when
        # unchanged); same gating rationale as evidence.
        if court_cases_touched:
            case._sync_courtcase_references(validated.get("court_cases") or [])

        # Same contract for the author byline.
        if authors_touched:
            case._sync_author_credits(validated.get("authors") or [])

        return None, affected_material_iris

    def _apply_state_transition(self, request, case, target_state):
        """Dispatch a state change to the model method that implements it.

        Every target dispatches to the model method that already implements +
        validates the transition (Case.validate() enforces BR-1..BR-4 on
        IN_REVIEW/PUBLISHED). No transition rule is re-implemented here; the
        permission gate was applied by the caller via can_transition_case_state.
        A model ValidationError -> 422 with field-keyed messages (mirroring the
        original submit() handling). Returns a Response to send, or None.
        """
        from_state = case.state
        try:
            if target_state == CaseState.IN_REVIEW:
                case.submit()
            elif target_state == CaseState.PUBLISHED:
                case.publish()
            elif target_state == CaseState.CLOSED:
                # Soft-delete (state -> CLOSED + versionInfo audit entry).
                case.delete()
            elif target_state == CaseState.DRAFT:
                # Un-submit / un-publish. No dedicated model method exists;
                # set DRAFT (lenient validation — only title), record the
                # audit entry, and save (mirrors submit()/publish()).
                case.state = CaseState.DRAFT
                case.validate()
                case.versionInfo = {
                    "action": "reverted_to_draft",
                    "datetime": timezone.now().isoformat(),
                }
                case.save()
            else:
                transaction.set_rollback(True)
                return Response(
                    {
                        "detail": (
                            f"Unsupported state transition target: {target_state}."
                        )
                    },
                    status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                )
        except ValidationError as exc:
            detail = getattr(exc, "message_dict", None) or {"detail": exc.messages}
            transaction.set_rollback(True)
            return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Record the transition in the append-only history log (actor + optional
        # reason). The reason travels in the ``X-Transition-Reason`` header so the
        # RFC-6902 body stays a pure patch and we stop overloading the internal
        # ``/notes`` field for return reasons. Inside the same atomic block as the
        # caller, so the log row and the state change commit or roll back together.
        reason = (request.headers.get("X-Transition-Reason") or "").strip()
        CaseStateChange.objects.create(
            case=case,
            from_state=from_state,
            to_state=case.state,
            actor=request.user if request.user.is_authenticated else None,
            reason=reason[:2000],  # defensive cap; TextField is unbounded
        )
        return None

    def partial_update(self, request, *args, **kwargs):
        """
        PATCH /api/cases/{id}/

        Accepts an RFC 6902 JSON Patch document and applies it against a writable
        snapshot of the case. The snapshot is validated after patching, then scalar
        fields are saved via a bulk UPDATE and M2M relations are updated with .set().

        Blocked paths (id, version, timestamps, versionInfo) are rejected
        before the patch is applied.
        """
        # get_object() raises DRF's Http404/NotFound (→ 404) when the case is
        # absent; the ViewSet's queryset already scopes visibility, so no manual
        # DoesNotExist handling is needed here.
        case = self.get_object()

        rejection, patch_ops = self._reject_before_patch(request, case)
        if rejection is not None:
            return rejection

        snapshot = self._build_snapshot(case)
        try:
            patched = jsonpatch.apply_patch(snapshot, patch_ops)
        except (jsonpatch.JsonPatchException, jsonpatch.JsonPointerException) as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        serializer = CasePatchSerializer(data=patched)
        if not serializer.is_valid():
            return Response(
                serializer.errors, status=status.HTTP_422_UNPROCESSABLE_ENTITY
            )

        validated = serializer.validated_data

        target_state = validated.get("state")
        if target_state is not None and not can_transition_case_state(
            request.user, case, target_state
        ):
            return Response(
                {"detail": "Permission denied for requested state transition."},
                status=status.HTTP_403_FORBIDDEN,
            )
        # All target states (IN_REVIEW / PUBLISHED / CLOSED / DRAFT) are
        # supported; each dispatches to the corresponding model method below.
        # can_transition_case_state gates the roles: v3 allows any content-staff
        # principal (superuser or Caseworker) to transition to ANY state — the
        # old Caseworker DRAFT<->IN_REVIEW confinement is retired.

        entities_touched = self._touches(patch_ops, "/entities")
        evidence_touched = self._touches(patch_ops, "/evidence")
        court_cases_touched = self._touches(patch_ops, "/court_cases")
        authors_touched = self._touches(patch_ops, "/authors")

        with transaction.atomic():
            # Re-check ``If-Match`` under a row lock, INSIDE the transaction that
            # writes. The check in _reject_before_patch runs before this block, so
            # on its own it is only a fast pre-filter: two clients holding the same
            # ETag can both pass it and then clobber each other, because every write
            # below is an unconditional UPDATE rather than one predicated on the
            # version the token was derived from. Taking the lock first serializes
            # them, so the second sees the first's committed ``updated_at`` and gets
            # a 412 instead of silently overwriting — which is the whole point of
            # the header on an endpoint whose /entities and /evidence ops replace
            # whole lists. Mirrors materials/views.py, the same contract on the
            # material plane.
            #
            # NOTE: sqlite has ``has_select_for_update = False``, so Django SILENTLY
            # omits FOR UPDATE under the test gate — the lock is real on Postgres
            # (prod) only. The tests therefore pin the If-Match contract, not the
            # lock itself.
            locked = (
                Case.objects.select_for_update().filter(pk=case.pk).only("updated_at")
            ).first()
            if locked is not None and not _if_match_matches(request, locked):
                transaction.set_rollback(True)
                resp = Response(
                    {
                        "detail": (
                            "This case was modified since you opened it. "
                            "Reload to get the latest version before saving."
                        )
                    },
                    status=status.HTTP_412_PRECONDITION_FAILED,
                )
                resp["ETag"] = _version_token(locked)
                return resp

            # Persist scalar field changes
            scalar_updates = {
                field: validated[field]
                for field in _PATCH_SCALAR_FIELDS
                if field in validated
            }
            if scalar_updates:
                case = self.get_object()
                # ``QuerySet.update()`` bypasses the model's ``auto_now`` on
                # ``updated_at`` (that only fires on ``save()``), so scalar content
                # edits would otherwise leave ``updated_at`` — and the derived
                # optimistic-concurrency token — stale. Bump it explicitly so the
                # serialized timestamp and the ETag both track content edits.
                # ``CaseQuerySet.update()`` records the slug change into
                # CaseSlugHistory when ``scalar_updates`` carries a new ``slug``,
                # so the retired slug's URL 301-redirects instead of 404ing
                # (BB-38) — no explicit ``record()`` call is needed here.
                Case.objects.filter(pk=case.pk).update(
                    updated_at=timezone.now(), **scalar_updates
                )
                # ``QuerySet.update()`` bypasses ``post_save``, so auditlog's
                # UPDATE receiver never fires for scalar content edits. ``Case``
                # carries the audited manager (jawafdehi_shared.db.audited), whose
                # ``update()`` override records the scalar diff (with the request
                # actor; the auto_now ``updated_at`` bump above is excluded) — so
                # this write is logged automatically, no explicit call needed.

            # Persist join changes (entities / evidence / court-case refs), each
            # only when its path was explicitly patched — avoids an unnecessary
            # delete/recreate on scalar-only PATCHes.
            case.refresh_from_db()
            join_error, affected_material_iris = self._write_joins(
                case,
                validated,
                (
                    entities_touched,
                    evidence_touched,
                    court_cases_touched,
                    authors_touched,
                ),
            )
            if join_error is not None:
                return join_error

            # Bump ``updated_at`` for a relation-only PATCH. The scalar path bumps
            # it above and every state transition re-saves the row, but a PATCH
            # that touches ONLY joins (/entities, /evidence, /court_cases) with no
            # scalar field and no state change writes through the join tables and
            # never touches the Case row — leaving ``updated_at`` (and the derived
            # ETag) stale, so a concurrent relation edit could clobber unseen.
            relations_touched = (
                entities_touched
                or evidence_touched
                or court_cases_touched
                or authors_touched
            )
            if relations_touched and not scalar_updates:
                Case.objects.filter(pk=case.pk).update(updated_at=timezone.now())

            case.refresh_from_db()

            if target_state is not None and target_state != case.state:
                transition_response = self._apply_state_transition(
                    request, case, target_state
                )
                if transition_response is not None:
                    return transition_response

        self._after_commit(
            case,
            affected_material_iris,
            evidence_touched=evidence_touched,
            state_changed=target_state is not None,
        )

        # ``case`` was refreshed after the writes above; a state transition also
        # re-saved it, so ``updated_at`` reflects the just-written row. Echo the
        # fresh optimistic-concurrency token so a client editing in place can
        # PATCH again without a re-fetch.
        case.refresh_from_db(fields=["updated_at"])
        response = Response(CaseSerializer(case).data, status=status.HTTP_200_OK)
        response["ETag"] = _version_token(case)
        return response

    def destroy(self, request, *args, **kwargs):
        """
        DELETE /api/cases/{id}/

        Soft-delete a case. Consistent with the platform's existing pattern
        (Case has no is_deleted flag; ``Case.delete()`` transitions state to
        CLOSED and the ViewSet already excludes CLOSED cases from every read),
        this transitions the case to CLOSED rather than hard-deleting it — the
        record is preserved for audit. Returns 204.

        Authorization mirrors PATCH (``can_change_case``) on top of the
        cases.delete_case model permission enforced by get_permissions().
        """
        # get_object() raises DRF's Http404/NotFound (→ 404) when the case is
        # absent; the ViewSet's queryset already scopes visibility, so no manual
        # DoesNotExist handling is needed here.
        case = self.get_object()

        if not can_change_case(request.user, case):
            return Response(
                {"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN
            )

        # Capture the referenced materials BEFORE the soft-delete so their
        # visibility is recomputed after: a CLOSED (soft-deleted) case must not
        # keep its evidence publicly LISTED (ADR draft-leak guard).
        referenced_iris = list(
            case.material_references.values_list("material_iri", flat=True)
        )

        # Case.delete() is overridden to soft-delete (state -> CLOSED + versionInfo
        # audit entry); it never hard-removes the row. The post-transition CLOSED
        # state is evicted from the search index by the case save signal.
        case.delete()

        _recompute_material_visibility(referenced_iris)

        return Response(status=status.HTTP_204_NO_CONTENT)

    def _build_snapshot(self, case: Case) -> dict:
        """Return a writable dict representing the patchable surface of a case."""
        return {
            "title": case.title,
            "state": case.state,
            "short_description": case.short_description,
            "description": case.description,
            "thumbnail_url": case.thumbnail_url,
            "banner_url": case.banner_url,
            "case_start_date": (
                str(case.case_start_date) if case.case_start_date else None
            ),
            "case_end_date": str(case.case_end_date) if case.case_end_date else None,
            "case_type": case.case_type,
            "tags": list(case.tags) if case.tags else [],
            "key_allegations": (
                list(case.key_allegations) if case.key_allegations else []
            ),
            "timeline": list(case.timeline) if case.timeline else [],
            # Evidence is now the CaseMaterialReference join (case.material_references),
            # not a JSON blob on Case. It is read-only in the patch snapshot for now;
            # material-reference writes move to a dedicated CaseMaterialReference path
            # in a follow-up.
            "evidence": [
                {
                    "material_iri": ref.material_iri,
                    "additional_details": ref.additional_details or "",
                    "ordinal": ref.ordinal,
                }
                for ref in case.material_references.all()
            ],
            "entities": [
                {
                    "nes_id": rel.nes_id,
                    "relationship_type": rel.relationship_type,
                    "outcome": rel.outcome,
                    "notes": rel.notes or "",
                }
                for rel in case.entity_relationships.all()
            ],
            "slug": case.slug,
            # Single property read: each access queries the reference join
            # unless prefetched.
            "court_cases": case.court_cases,
            "missing_details": case.missing_details,
            "bigo": case.bigo,
            # Coerce a NULL read-back to 0 for the same reason as notes below.
            "weight": case.weight or 0,
            # Internal casework notes (BB-04-gated on read; casework-only). This
            # is the top-level case ``notes`` TextField, distinct from the nested
            # per-entity relationship ``notes`` above. Carried here so the editor's
            # ``replace /notes`` JSON-Patch op resolves against an existing key
            # (BB-28) and the scalar-field save below can persist it. Coerce a
            # ``None`` (the column is NOT NULL / ``default=""``, but a legacy or
            # raw row could still read back NULL) to "" so a snapshot value never
            # trips ``CasePatchSerializer.notes`` (``allow_blank`` but not
            # ``allow_null``) and 422s an otherwise-unrelated PATCH.
            "notes": case.notes or "",
            # Public notes (Case.public_notes TextField, NOT NULL / default="").
            # Coerce a NULL read-back to "" for the same reason as notes above.
            "public_notes": case.public_notes or "",
            # The structured byline. ``case_publish_date`` is stringified like the
            # other dates above so the patched snapshot round-trips through
            # ``CasePatchSerializer.DateField``; None stays None (the column is
            # nullable and a DRAFT legitimately has no publish date).
            "case_publish_date": (
                str(case.case_publish_date) if case.case_publish_date else None
            ),
            "public_edit_history": (
                list(case.public_edit_history) if case.public_edit_history else []
            ),
            # The byline's ONLY per-case fact is order, so the writable shape is
            # a plain ordered list of account ids. Display details (name, photo,
            # description) are per-person and come back on the READ serializer,
            # resolved from each author's AuthorProfile.
            "authors": case.author_ids,
        }


@extend_schema(
    summary="Get a public author profile",
    description=(
        "An author's public profile and the cases they wrote, newest first. "
        "Public. 404s unless the profile is published (`has_public_page`) — a "
        "profile row is created automatically the first time someone is "
        "credited, so an unpublished one is an empty placeholder, not a page."
    ),
    responses={200: AuthorProfileDetailSerializer},
    tags=["cases"],
)
class AuthorProfileView(RetrieveAPIView):
    """GET /api/authors/<slug>/ — a public author profile page.

    Only PUBLISHED cases are listed. A caseworker's draft is not public
    elsewhere, and an author page must not become the one place a draft's
    existence leaks.

    Ordered by ``case_publish_date`` descending — the date the case actually went
    live, not ``created_at`` (which is when the row was typed in, routinely
    months later). Cases with no publish date sort last rather than first, which
    is what ``F(...).desc(nulls_last=True)`` buys over a plain ``-`` prefix.
    """

    serializer_class = AuthorProfileDetailSerializer
    permission_classes = [AllowAny]
    lookup_field = "slug"

    def get_queryset(self):
        return AuthorProfile.objects.filter(has_public_page=True).select_related("user")

    def retrieve(self, request, *args, **kwargs):
        profile = self.get_object()
        cases = (
            profile.user.authored_cases.filter(state=CaseState.PUBLISHED)
            .order_by(F("case_publish_date").desc(nulls_last=True), "-created_at")
        )
        data = self.get_serializer(profile).data
        data["cases"] = AuthorCaseSummarySerializer(cases, many=True).data
        return Response(data)


@extend_schema(
    summary="List case-author candidates",
    description=(
        "The accounts that may be credited as a case author, for the byline "
        "picker in the case editor. Casework-role only. Optional `?search=` "
        "matches username, first name and last name (case-insensitive, "
        "substring)."
    ),
    parameters=[
        OpenApiParameter(
            name="search",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            required=False,
            description="Filter candidates by username or name.",
        )
    ],
    responses={200: CaseAuthorCandidateSerializer(many=True)},
    tags=["cases"],
)
class CaseAuthorCandidateView(ListAPIView):
    """GET /api/case-authors/ — accounts creditable as a case author.

    Exists because ``CaseAuthor.user`` is a REQUIRED foreign key: a byline can
    only name a real account, so the editor needs the roster to pick from. There
    was no user-listing endpoint on this API before.

    ACTIVE accounts only. Deactivating someone removes them from the picker but
    does NOT touch bylines they already carry — those are ``PROTECT``-ed rows
    holding a snapshotted ``display_name``.

    Deliberately UNPAGINATED. The default page size is 20, and a picker that
    silently stops at the 20th colleague is the kind of bug nobody reports for a
    year; the staff table is small and bounded. If it ever isn't, add pagination
    here AND teach the picker to page — don't let the default do it quietly.
    """

    serializer_class = CaseAuthorCandidateSerializer
    permission_classes = [IsCaseAuthorPicker]
    pagination_class = None

    def get_queryset(self):
        # select_related so the serializer's profile-name lookup doesn't fan out
        # into one query per candidate.
        queryset = User.objects.filter(is_active=True).select_related("author_profile")
        search = (self.request.query_params.get("search") or "").strip()
        if search:
            queryset = queryset.filter(
                Q(username__icontains=search)
                | Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
            )
        # Order by the name the byline will actually show, so the picker reads in
        # the same order the credits do.
        return queryset.order_by("first_name", "last_name", "username")


@extend_schema(
    summary="Get case statistics",
    description="""
    Retrieve aggregate statistics about cases in the system.

    Returns:
    - `published_cases`: Number of cases with state PUBLISHED
    - `cases_under_investigation`: Number of cases with state DRAFT or IN_REVIEW
    - `cases_in_review`: Number of cases with state IN_REVIEW (subset of
      under-investigation — cases being prepared for publication)
    - `cases_closed`: Number of cases with state CLOSED
    - `cases_ciaa`: Number of CIAA corruption cases (case_type CORRUPTION)
    - `cases_non_ciaa`: Number of cases handled outside CIAA (all other types)
    - `entities_tracked`: Number of unique entities involved in published cases
    - `total_bigo`: Sum of the bigo (बिगो — the disputed/embezzled amount, NPR)
      across published cases; 0 when no published case carries an amount
    - `nes`: NES (entities) coverage — total, by-prefix / by-type breakdowns,
      persons-by-sector (`persons_by_sector`, derived from the office each person
      holds), and completeness percentages (identifier / provenance / bilingual
      name)
    - `ngm`: NGM (judicial) coverage — court-case / court totals, by-court-type
      breakdown, court-cases-per-year (`by_year`) and per-court-level-per-year
      (`by_court_type_year`), and completeness percentages (NES-resolved /
      registration date / document sources). Both per-year breakdowns are keyed
      by `bs_year` — the **Bikram Sambat** registration year taken from the court
      register, not a Gregorian one (BS 2081 runs mid-April 2024 to mid-April
      2025), covering the most recent 25 years on record
    - `materials`: NGM materials (development-project / document dataset) coverage —
      total, by-type / by-source breakdowns, and completeness percentages
      (description / url / date)
    - `last_updated`: Timestamp when statistics were last calculated

    **Caching:**
    - Statistics are precomputed asynchronously on a schedule (every 5 minutes)
      and served from a shared snapshot, so values may be a few minutes stale
    - Responses are publicly cacheable (`Cache-Control: public, max-age=60,
      s-maxage=300`) and served from the CDN edge, so end-to-end staleness can
      reach ~10 minutes worst case
    - `last_updated` is the time the served snapshot was computed
    """,
    tags=["statistics"],
    responses={
        200: {
            "type": "object",
            "properties": {
                "published_cases": {"type": "integer", "example": 127},
                "cases_under_investigation": {"type": "integer", "example": 43},
                "cases_in_review": {"type": "integer", "example": 12},
                "cases_closed": {"type": "integer", "example": 31},
                "cases_ciaa": {"type": "integer", "example": 88},
                "cases_non_ciaa": {"type": "integer", "example": 39},
                "entities_tracked": {"type": "integer", "example": 89},
                "total_bigo": {"type": "integer", "example": 4519830000},
                "nes": {"type": "object", "description": "NES coverage metrics"},
                "ngm": {
                    "type": "object",
                    "description": "NGM judicial coverage metrics",
                },
                "materials": {
                    "type": "object",
                    "description": "NGM materials coverage metrics",
                },
                "last_updated": {
                    "type": "string",
                    "format": "date-time",
                    "example": "2024-12-04T10:30:00Z",
                },
            },
        }
    },
)
class StatisticsView(APIView):
    """
    Public API endpoint for case statistics.

    Serves the precomputed ``StatisticsSnapshot`` row — a single primary-key
    lookup per request. The heavy NES/NGM aggregation runs out-of-band in the
    ``refresh_statistics`` management command on a schedule; see
    ``cases.services.statistics`` for the computation and the rationale.

    The payload is anonymous-public and identical for everyone, so real
    (non-placeholder) responses are marked publicly cacheable: ``s-maxage``
    lets the CDN edge (a Cloudflare cache rule marks this path eligible)
    absorb the fan-out for 5 minutes, ``max-age`` gives browsers a short
    hold. Combined with the 5-minute snapshot refresh, worst-case staleness
    is ~10 minutes — acceptable for aggregate statistics.
    """

    # Kept in lockstep with the refresh cadence: edge TTL == the CronJob's
    # 5-minute schedule, so edge staleness never exceeds one refresh interval.
    CACHE_CONTROL = "public, max-age=60, s-maxage=300"

    def get(self, request):
        """Serve the shared precomputed statistics snapshot (O(1) PK lookup)."""
        snapshot = StatisticsSnapshot.objects.filter(pk=STATISTICS_SNAPSHOT_KEY).first()
        if snapshot is not None:
            # A bootstrap-placeholder row (committed by the claim below while
            # the winning request is still computing) must never be pinned at
            # the edge — its zeroed blocks would be served worldwide for a
            # full TTL. Real snapshots are publicly cacheable.
            cache_control = (
                "no-store" if snapshot.is_placeholder else self.CACHE_CONTROL
            )
            return Response(snapshot.data, headers={"Cache-Control": cache_control})
        # Bootstrap: no snapshot row yet (fresh database, before the first
        # scheduled refresh has run). Claim the row with an atomic INSERT so
        # exactly ONE request pays the aggregation; concurrent requests that
        # lose the claim serve a cheap placeholder instead of stacking
        # multi-second recomputes (thundering-herd guard). If the winner dies
        # mid-compute, the placeholder row persists until the next scheduled
        # refresh overwrites it.
        placeholder = bootstrap_placeholder()
        try:
            with transaction.atomic():
                StatisticsSnapshot.objects.create(
                    key=STATISTICS_SNAPSHOT_KEY,
                    data=placeholder,
                    computed_at=timezone.now(),
                    is_placeholder=True,
                )
        except IntegrityError:
            # The placeholder's zeroed blocks must never be pinned at the
            # edge for a full TTL — this response is for THIS request only.
            return Response(placeholder, headers={"Cache-Control": "no-store"})
        return Response(
            refresh_statistics(), headers={"Cache-Control": self.CACHE_CONTROL}
        )


class FeedbackRateThrottle(AnonRateThrottle):
    """Rate throttle for feedback submissions: 5 per hour, in its OWN bucket.

    ``scope`` is set explicitly. Without it this inherits ``AnonRateThrottle``'s
    ``scope = "anon"``, and DRF derives the cache key from the scope alone
    (``throttle_anon_<ident>``) — so this 5/hour throttle would share one history
    list with the GLOBAL ``SyncedAnonRateThrottle``, which also has scope
    ``anon`` and runs on every other public endpoint at 1000/hour. A visitor who
    had merely browsed the site (5+ anonymous API calls in the past hour) would
    then be refused on their FIRST feedback or corruption-report submission,
    because those unrelated reads had already filled this bucket.

    ``NewsletterRateThrottle`` sets ``scope = "newsletter"`` for exactly this
    reason; the omission here was an oversight rather than a decision.

    Not caught by the existing tests because the global throttle classes are
    emptied under ``TESTING`` (config/settings.py), so the two never collide
    there — see ``test_feedback_throttle_scope.py``, which asserts on the cache
    key instead of trying to reproduce the collision.
    """

    scope = "feedback"
    rate = "5/hour"


def _notify_case_report(feedback) -> None:
    """Best-effort alert to the casework inbox that a report has landed.

    Reports are now visible in the SPA admin panel's feedback queue
    (``FeedbackTriageViewSet``), but nothing polls it, so without this mail a
    corruption report waits until someone happens to look. Django's mail backend
    is the dummy one on this platform (it accepts and discards), so SendPulse's
    transactional endpoint is the only path that actually sends.

    The mail carries a reference number and an admin link, and nothing else. Not
    the subject, not the description, not whether contact details or an
    attachment were supplied — presence metadata still tells a reader of the
    mailbox something about the source. Everything stays in the database, so a
    third-party mail system never holds any part of a whistleblower's account.

    Never raises: a failed notification must not fail the submission, or the
    reporter sees an error and may not try again.
    """
    if not getattr(settings, "CASE_REPORT_NOTIFY", False):
        return
    recipient = getattr(settings, "CASE_REPORT_NOTIFY_EMAIL", "")
    if not recipient:
        return
    # This points at the SPA feedback queue, not Django admin. The recipient is
    # the casework inbox, and a caseworker has no Django admin feedback
    # permission at all (``create_groups`` grants none) — the old link 403'd for
    # exactly the people it was sent to. The SPA queue is gated on the Caseworker
    # role, so it opens for them.
    #
    # An EMPTY setting falls back to the default rather than suppressing the
    # mail, matching ``case_events.notify._base_url`` — the other consumer of
    # this setting, which also builds an /admin/... deep link. os.getenv's
    # default only applies when the variable is ABSENT, so a ConfigMap key
    # rendered as "" would otherwise reach us blank; the two consumers must not
    # disagree about what blank means. Silence is the worst failure mode here:
    # nothing polls the queue, so a suppressed mail means the report is simply
    # never read.
    base = (getattr(settings, "FRONTEND_BASE_URL", "") or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        base = "https://jawafdehi.org"
    try:
        from newsletter.sendpulse import get_client

        client = get_client()
        if client is None or not client.can_send_email:
            return
        admin_url = f"{base}/admin/feedback/{feedback.pk}"
        html = (
            "<p>A corruption case report was submitted through the website.</p>"
            f"<p>Reference: <strong>#{feedback.pk}</strong></p>"
            "<p>Nothing about the report is included in this email. Read it here:<br>"
            f'<a href="{admin_url}">{admin_url}</a></p>'
        )
        client.send_email(recipient, f"New case report #{feedback.pk}", html)
    except Exception as exc:  # noqa: BLE001 — notification is best-effort
        logger.warning(
            "Case report notification failed (submission #%s was still saved): %s",
            feedback.pk,
            exc,
        )


@extend_schema(
    summary="Submit platform feedback",
    description="""
    Submit feedback, bug reports, feature requests, or general comments about the platform.

    Rate limited to 5 submissions per IP address per hour.
    Contact information is optional - anonymous submissions are welcome.

    An optional file attachment may be included (max 10 MB). Submit as
    ``multipart/form-data`` when attaching a file; use ``application/json``
    for text-only submissions.
    """,
    request={
        "application/json": FeedbackSerializer,
        "multipart/form-data": FeedbackSerializer,
    },
    responses={
        201: FeedbackSerializer,
        400: OpenApiTypes.OBJECT,
        429: OpenApiTypes.OBJECT,
    },
    examples=[
        OpenApiExample(
            "Bug Report",
            value={
                "feedbackType": "bug",
                "subject": "Search not working on Cases page",
                "description": "When I try to search for cases, nothing happens.",
                "relatedPage": "Cases page",
                "contactInfo": {
                    "name": "राम बहादुर",
                    "contactMethods": [{"type": "email", "value": "ram@example.com"}],
                },
            },
            request_only=True,
        ),
        OpenApiExample(
            "Anonymous Feedback",
            value={
                "feedbackType": "general",
                "subject": "Great platform",
                "description": "This platform is very helpful!",
            },
            request_only=True,
        ),
    ],
)
class FeedbackView(APIView):
    """API view for submitting platform feedback."""

    # Public, unauthenticated submission endpoint (abuse is bounded by
    # FeedbackRateThrottle). Declared explicitly because the consolidated
    #  settings default to ReadOnlyOrAuthenticatedWrite, which would
    # otherwise 401 the anonymous POST; the former standalone Jawafdehi settings
    # set no global permission default (DRF AllowAny), so feedback was public.
    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [FeedbackRateThrottle]
    parser_classes = [JSONParser, MultiPartParser, FormParser]

    def post(self, request):
        """Handle feedback submission."""
        serializer = FeedbackSerializer(data=request.data)

        if serializer.is_valid():
            is_case_report = (
                serializer.validated_data.get("feedback_type")
                == FeedbackType.CASE_REPORT
            )

            # Corruption reports are stored without their reporter's IP or user
            # agent. Someone naming an official is exposed by those two fields in
            # a way a bug report's author is not, and the anonymity offered by the
            # form would be hollow if we kept them. Throttling is unaffected:
            # AnonRateThrottle derives the caller's identity from the request
            # itself and never reads the column.
            feedback = serializer.save(
                ip_address=None if is_case_report else self.get_client_ip(request),
                user_agent=(
                    "" if is_case_report else request.META.get("HTTP_USER_AGENT", "")
                ),
            )

            if is_case_report:
                _notify_case_report(feedback)

            return Response(
                serializer.to_representation(feedback), status=status.HTTP_201_CREATED
            )

        return Response(
            {"error": "Validation error", "details": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

    def get_client_ip(self, request):
        """Extract client IP address from request."""
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        if x_forwarded_for:
            ip = x_forwarded_for.split(",")[0].strip()
        else:
            ip = request.META.get("REMOTE_ADDR")
        return ip


@extend_schema_view(
    list=extend_schema(
        summary="List feedback submissions (staff)",
        description=(
            "Staff-facing queue of everything submitted through the public feedback "
            "and case-report forms. Requires the Caseworker role or superuser.\n\n"
            "The reporter's contact details, IP address and user agent are NOT "
            "returned by this endpoint — only whether contact details exist. "
            "Retrieving them remains a superuser action in Django admin."
        ),
    ),
    retrieve=extend_schema(
        summary="Retrieve one feedback submission (staff)",
        description="One submission, in the same PII-free shape as the list.",
    ),
    partial_update=extend_schema(
        summary="Triage a feedback submission (staff)",
        description=(
            "Update the workflow ``status``, the internal ``adminNotes``, and/or "
            "``feedbackType`` to reclassify a mis-filed submission. Everything "
            "the reporter wrote is read-only."
        ),
    ),
)
class FeedbackTriageViewSet(
    AuditlogActorMixin,
    mixins.UpdateModelMixin,
    viewsets.ReadOnlyModelViewSet,
):
    """Staff read + triage surface for feedback, at ``/api/feedback-submissions/``.

    Every operation carries an explicit ``description`` above. That is
    load-bearing, not decoration: drf-spectacular falls back to THIS docstring
    for any operation without one, and ``/api/schema/`` is served with
    drf-spectacular's default ``SERVE_PERMISSIONS`` of ``AllowAny``. Anything
    written here is world-readable, so the reasoning below stays in comments.
    """

    # Why this is a separate route rather than a GET added to the public
    # ``FeedbackView``: that view sets ``authentication_classes = []`` so an
    # anonymous reporter can always POST. With no authenticator running,
    # ``request.user`` is always anonymous there and a staff GET could never be
    # identified. Restoring authentication to serve a read would also mean a
    # stale bearer token in a reporter's browser could 401 the public form — a
    # regression paid by the person least able to work around it.
    #
    # ``AuditlogActorMixin`` binds the DRF-authenticated user so a triage edit is
    # attributed to whoever made it. Note the trail is NOT a privacy control:
    # auditlog's ``mask_fields`` (cases/apps.py) replaces only the first half of
    # a value, so a masked ``contact_info`` still shows the tail of an email
    # address. The control that matters is the serializer's field list.
    serializer_class = FeedbackTriageSerializer
    permission_classes = [IsFeedbackTriager]
    pagination_class = CasePagination
    filter_backends = [
        DjangoFilterBackend,
        filters.SearchFilter,
        filters.OrderingFilter,
    ]
    filterset_fields = ["status", "feedback_type"]
    search_fields = ["subject", "description", "related_page"]
    ordering_fields = ["submitted_at", "updated_at", "status", "feedback_type"]
    ordering = ["-submitted_at"]
    # No PUT: only three fields are writable, so a full replace has no meaning
    # here and would invite a client to send back the read-only reporter fields.
    http_method_names = ["get", "patch", "head", "options"]

    # Defence in depth on the READ path only, and it is worth being precise
    # about how little that is worth: ``defer`` is a laziness hint, NOT an access
    # boundary. Django loads a deferred column transparently on first attribute
    # access, so a PII field added to the serializer by mistake would still be
    # served — just with an extra query per row. The control that actually holds
    # is ``FeedbackTriageSerializer``'s field list, guarded by the PII_KEYS
    # assertions in tests/api/test_feedback_triage.py.
    #
    # It does hold on this path because nothing here touches the two columns.
    # It does NOT survive ``Model.save()``, which is one of the reasons the
    # serializer writes via a column-scoped UPDATE instead (see its ``update``).
    queryset = Feedback.objects.defer("ip_address", "user_agent")


OEMBED_URL_PATTERN = re.compile(
    r"^https?://(?:www\.)?jawafdehi\.org/(?P<kind>case|updates|entity)/(?P<ref>[^?#]+?)/?(?:[?#].*)?$"
)
EMBED_BASE_URL = "https://jawafdehi.org"
DEFAULT_EMBED_WIDTH = 600
DEFAULT_EMBED_HEIGHT = 300


def _text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for lang in ("en", "ne"):
            val = value.get(lang)
            if isinstance(val, str) and val.strip():
                return val
        return next((v for v in value.values() if isinstance(v, str) and v.strip()), "")
    if isinstance(value, list):
        for item in value:
            text = _text(item).strip()
            if text:
                return text
    return ""


def _iframe_html(src, title, width, height):
    return (
        f'<iframe src="{escape(src, quote=True)}" '
        f'width="{width}" '
        f'height="{height}" '
        f'frameborder="0" '
        f'allowtransparency="true" '
        f'scrolling="no" '
        f'style="border:0;overflow:hidden;max-width:100%;" '
        f'title="{escape(title, quote=True)}">'
        f"</iframe>"
    )


@extend_schema(
    summary="oEmbed endpoint",
    description="""
    oEmbed provider endpoint for public Jawafdehi share pages.

    When a journalist pastes a Jawafdehi URL into Substack, Medium,
    WordPress, or any oEmbed-compatible platform, the platform discovers
    this endpoint and requests an embeddable widget.

    **Parameters:**
    - `url` (required): a supported jawafdehi.org share URL to embed
    - `format` (optional): response format — `json` (default) or `xml`

    Supported URLs are public case pages, live update pages, and public entity
    registry pages.
    Returns a `rich` type embed with an iframe pointing to the embed card.
    """,
    parameters=[
        OpenApiParameter(
            name="url",
            type=OpenApiTypes.URI,
            location=OpenApiParameter.QUERY,
            description="Full jawafdehi.org share URL to embed",
            required=True,
        ),
        OpenApiParameter(
            name="format",
            type=OpenApiTypes.STR,
            location=OpenApiParameter.QUERY,
            description="Response format: json or xml",
            enum=["json", "xml"],
            required=False,
        ),
    ],
    tags=["oembed"],
    responses={
        200: OpenApiTypes.OBJECT,
        400: OpenApiTypes.OBJECT,
        404: OpenApiTypes.OBJECT,
    },
)
class OEmbedView(APIView):
    """
    oEmbed provider endpoint.

    GET /api/oembed/?url=https://jawafdehi.org/case/{slug}
    GET /api/oembed/?url=https://jawafdehi.org/updates/{slug}
    GET /api/oembed/?url=https://jawafdehi.org/entity/{prefix}/{slug}

    Extracts the shareable resource ref from the provided URL, looks up the
    published resource, and returns an oEmbed response with an iframe embed code.
    """

    authentication_classes = []
    permission_classes = []

    def perform_content_negotiation(self, request, force=False):
        # oEmbed uses 'format' as a query param per the oEmbed spec.
        # Prevent DRF from intercepting it for content negotiation,
        # which would raise Http404 when format != 'json'.
        renderer = JSONRenderer()
        return (renderer, renderer.media_type)

    def get(self, request):
        response_format = request.query_params.get("format", "json").lower()

        url = request.query_params.get("url", "").strip()
        if not url:
            return Response(
                {"error": "Missing required parameter: url"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        match = OEMBED_URL_PATTERN.match(url)
        if not match:
            return Response(
                {
                    "error": (
                        "URL does not match a supported Jawafdehi pattern. "
                        "Expected: https://jawafdehi.org/case/{slug}, "
                        "https://jawafdehi.org/updates/{slug}, or "
                        "https://jawafdehi.org/entity/{prefix}/{slug}"
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if response_format not in ("json", "xml"):
            return Response(
                {"error": f"Unsupported format: {response_format}"},
                status=status.HTTP_501_NOT_IMPLEMENTED,
            )

        width = self._parse_dimension(
            request.query_params.get("maxwidth"), DEFAULT_EMBED_WIDTH
        )
        height = self._parse_dimension(
            request.query_params.get("maxheight"), DEFAULT_EMBED_HEIGHT
        )

        ref = unquote(match.group("ref")).strip("/")
        if match.group("kind") == "case":
            oembed_data = self._case_oembed(ref, width, height)
        elif match.group("kind") == "updates":
            oembed_data = self._update_oembed(ref, width, height)
        else:
            oembed_data = self._entity_oembed(ref, width, height)

        if oembed_data is None:
            return Response(
                {"error": "Resource not found or not published."},
                status=status.HTTP_404_NOT_FOUND,
            )

        if response_format == "xml":
            return self._xml_response(oembed_data)

        return Response(oembed_data)

    def _base_oembed(
        self,
        *,
        title,
        embed_url,
        width,
        height,
        thumbnail_url="",
        thumbnail_width=None,
        thumbnail_height=None,
    ):
        return {
            "type": "rich",
            "version": "1.0",
            "title": title,
            "author_name": "Jawafdehi Editorial",
            "author_url": EMBED_BASE_URL,
            "provider_name": "Jawafdehi",
            "provider_url": EMBED_BASE_URL,
            "cache_age": 3600,
            "html": _iframe_html(embed_url, title, width, height),
            "width": width,
            "height": height,
            "thumbnail_url": thumbnail_url or "",
            "thumbnail_width": thumbnail_width if thumbnail_url else None,
            "thumbnail_height": thumbnail_height if thumbnail_url else None,
        }

    def _case_oembed(self, slug, width, height):
        if "/" in slug:
            return None
        try:
            case = Case.objects.get(slug=slug, state=CaseState.PUBLISHED)
        except Case.DoesNotExist:
            return None

        return self._base_oembed(
            title=case.title,
            embed_url=f"{EMBED_BASE_URL}/embed/case/{slug}",
            width=width,
            height=height,
            thumbnail_url=case.thumbnail_url or "",
        )

    def _update_oembed(self, slug, width, height):
        if "/" in slug:
            return None
        from content.models import ArticlePage

        article = ArticlePage.objects.live().public().filter(slug=slug).first()
        if article is None:
            return None

        thumbnail_url = ""
        thumbnail_width = None
        thumbnail_height = None
        if article.thumbnail_id:
            try:
                rendition = article.thumbnail.get_rendition("fill-800x450")
                thumbnail_url = absolute_media_url(rendition.url)
                thumbnail_width = rendition.width
                thumbnail_height = rendition.height
            except Exception:  # pragma: no cover  # noqa: BLE001 - a rendition failure degrades to no thumbnail
                thumbnail_url = ""

        return self._base_oembed(
            title=article.title,
            embed_url=f"{EMBED_BASE_URL}/embed/updates/{slug}",
            width=width,
            height=height,
            thumbnail_url=thumbnail_url,
            thumbnail_width=thumbnail_width,
            thumbnail_height=thumbnail_height,
        )

    def _entity_oembed(self, ref, width, height):
        if "/" not in ref:
            return None

        from entities.persistence import EntityRepository
        from jawafdehi_shared.entities.ids import build_entity_iri

        prefix, _, slug = ref.rpartition("/")
        try:
            iri = build_entity_iri(prefix, slug)
        except ValueError:
            return None

        entity = EntityRepository().get_entity(iri)
        if entity is None:
            return None

        title = _text(entity.get("name")) or slug.replace("-", " ").title()

        return self._base_oembed(
            title=title,
            embed_url=f"{EMBED_BASE_URL}/embed/entity/{ref}",
            width=width,
            height=height,
        )

    def _parse_dimension(self, raw, default):
        if raw is None:
            return default
        try:
            val = int(raw)
        except (ValueError, TypeError):
            return default
        if val <= 0:
            return default
        return val

    def _xml_response(self, data):
        root = Element("oembed")

        for key, value in data.items():
            if value is None:
                continue
            if isinstance(value, int):
                value = str(value)
            child = SubElement(root, key)
            child.text = value

        xml_str = '<?xml version="1.0" encoding="utf-8"?>\n' + tostring(
            root, encoding="unicode"
        )
        return HttpResponse(xml_str, content_type="text/xml")


class MeView(APIView):
    """Resolve the calling chat identity to a Jawafdehi user.

    Called by the jawafdehi-mcp server (GET /api/caseworker/me) using the
    Zitadel service-account OIDC access token plus an X-Jawafdehi-User-Id
    header. Auth: inherits the OIDC-only DEFAULT_AUTHENTICATION_CLASSES (no
    per-view pin), so `request.user` is the service-account principal keyed on
    its OIDC `sub` and `request.auth` is the decoded claims dict.

    A Zitadel service account is indistinguishable from a human at the
    transport layer, so the caller is recognised out-of-band: its `sub` must be
    in settings.OIDC_SERVICE_ACCOUNT_SUBJECTS.
    """

    def _is_service_account(self, request):
        user = request.user
        if not (user and user.is_authenticated):
            return False
        allowed_subjects = set(
            getattr(settings, "OIDC_SERVICE_ACCOUNT_SUBJECTS", []) or []
        )
        return user.username in allowed_subjects

    def get(self, request):
        if not request.user.is_authenticated:
            return Response(
                {"error": "Authentication required"},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not self._is_service_account(request):
            return Response(
                {"error": "Service account credentials required"},
                status=status.HTTP_403_FORBIDDEN,
            )

        owui_user_id = (request.META.get(JAWAFDEHI_USER_ID_HEADER) or "").strip()
        if not owui_user_id:
            return Response(
                {"error": "X-Jawafdehi-User-Id header is required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        identity = resolve_or_create_identity(owui_user_id, request)
        if identity is None:
            return Response(
                {"error": f"Unknown user: {owui_user_id}"},
                status=status.HTTP_404_NOT_FOUND,
            )

        real_user = identity.user

        if real_user is None:
            return Response(
                {
                    "mapped": False,
                    "owui_user_id": identity.owui_user_id,
                    "owui_user_name": identity.owui_user_name,
                    "message": "Chat identity is not yet mapped to a Jawafdehi user. An admin must link this identity in the admin panel.",
                },
                status=status.HTTP_200_OK,
            )

        if not real_user.is_active:
            return Response(
                {"error": "User account is inactive"},
                status=status.HTTP_403_FORBIDDEN,
            )

        roles = list(real_user.groups.values_list("name", flat=True))

        return Response(
            {
                "mapped": True,
                "roles": roles,
                # v3: admin == Django superuser (no group), so ``roles`` is empty
                # for an admin — admin-ness is carried by ``is_admin``. Mirrors
                # review.views._user_roles_payload so both "me" surfaces agree.
                "is_admin": real_user.is_superuser,
                "user_id": real_user.id,
                "username": real_user.get_username(),
                "owui_user_id": identity.owui_user_id,
                "owui_user_name": identity.owui_user_name,
            }
        )
