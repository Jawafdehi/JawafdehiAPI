"""
API ViewSets for the Jawafdehi accountability platform.

See: .kiro/specs/accountability-platform-core/design.md
"""

import logging
import re
from xml.etree.ElementTree import Element, SubElement, tostring

import jsonpatch
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
)
from rest_framework import filters, mixins, status, viewsets
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, DjangoModelPermissions, IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from jawafdehi_shared.identity import (
    JAWAFDEHI_USER_ID_HEADER,
    resolve_or_create_identity,
)

# NES + NGM models live in sibling apps; the DB router (config.db_router)
# sends each to its own database on read, so the cases app can query them directly
# for the cross-source data-quality metrics surfaced by StatisticsView.
from entities.models import StoredEntity
from courts.models import Court, CourtCase
from materials.models import Material

from .admin import CaseAdminForm
from .caseworker_serializers import (
    BLOCKED_PATH_PREFIXES,
    CaseCreateSerializer,
    CasePatchSerializer,
)
from .models import (
    Case,
    CaseEntityRelationship,
    CaseState,
    DocumentSource,
    RelationshipType,
)
from .rules.predicates import (
    can_change_case,
    can_transition_case_state,
    can_view_case,
    is_admin_or_moderator,
    is_caseworker,
    is_readonly,
)
from .serializers import (
    CaseDetailSerializer,
    CaseSerializer,
    DocumentSourceSerializer,
    FeedbackSerializer,
)

logger = logging.getLogger(__name__)


# NOTE: the former Jawafdehi-scoped ``UnifiedSearchView`` (an in-process ORM
# search over cases/entities/documents) was REMOVED in the unified-search cutover
# (plan decision #5: OpenSearch is the one-way substrate, no in-process fallback).
# Platform search now lives in the ``search`` app at ``GET /api/search/`` (see
# ``search``), which queries all four OpenSearch indices.


@extend_schema_view(
    create=extend_schema(
        summary="Create a draft case",
        description="""
        Create a new case through the same validation rules used by the Django admin form.

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
        - Admin / Moderator / Caseworker / ReadOnly: all non-CLOSED cases
          (PUBLISHED + IN_REVIEW + DRAFT).
        - Other authenticated users: PUBLISHED cases + any DRAFT or IN_REVIEW cases
          they are explicitly assigned to as contributors.

        Results are ordered by creation date (newest first).

        **Filtering:**
        - `case_type`: Filter by case type (CORRUPTION)
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
                name="tags",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Filter cases containing this tag",
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
        - PUBLISHED cases: accessible to everyone (public)
        - DRAFT and IN_REVIEW cases (casework): require a casework-viewing role
          (ReadOnly / Caseworker / Moderator / Admin, or an assigned contributor);
          anonymous/public callers get 404
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
class CaseViewSet(viewsets.ReadOnlyModelViewSet):
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

    Read visibility is role-based: unauthenticated/public callers see PUBLISHED
    cases only (DRAFT and IN_REVIEW are casework → 404 for them). Admin /
    Moderator / Caseworker / ReadOnly see all non-CLOSED cases (incl. DRAFT and
    IN_REVIEW). CLOSED cases are never exposed via this API.
    """

    serializer_class = CaseSerializer
    lookup_field = "slug"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["case_type"]
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
          - Unauthenticated/public: PUBLISHED only (DRAFT/IN_REVIEW → 404)
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
            # Casework (DRAFT + IN_REVIEW) is viewable only by casework roles
            # (readonly/caseworker/moderator/admin) — NOT the public. Anonymous /
            # public callers see only PUBLISHED. (Role model: public = readonly
            # EXCEPT no casework access; the per-object check is in retrieve().)
            if self.request.user and self.request.user.is_authenticated:
                # Exclude CLOSED cases from the API; the retrieve() gate enforces
                # casework-role for non-PUBLISHED states.
                queryset = Case.objects.exclude(state=CaseState.CLOSED)
            else:
                # Unauthenticated: PUBLISHED only (no in-review/draft casework).
                queryset = Case.objects.filter(state=CaseState.PUBLISHED)
        else:
            # List endpoint: visibility depends on authentication/role.
            # - Unauthenticated: PUBLISHED only
            # - Admin/Moderator/Caseworker/ReadOnly: all non-CLOSED cases
            # - Other authenticated: PUBLISHED + cases they are assigned to
            if not (self.request.user and self.request.user.is_authenticated):
                queryset = Case.objects.filter(state=CaseState.PUBLISHED)
            elif (
                is_admin_or_moderator(self.request.user)
                or is_caseworker(self.request.user)
                or is_readonly(self.request.user)
            ):
                queryset = Case.objects.exclude(state=CaseState.CLOSED)
            else:
                queryset = (
                    Case.objects.exclude(state=CaseState.CLOSED)
                    .filter(
                        Q(state=CaseState.PUBLISHED) | Q(contributors=self.request.user)
                    )
                    .distinct()
                )

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

        return queryset.prefetch_related(
            "entity_relationships",
        ).order_by("-created_at")

    def create(self, request, *args, **kwargs):
        """
        POST /api/cases/

        Create a new case by delegating validation to the existing Django admin form
        so API and admin creation semantics stay aligned.
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

        form = CaseAdminForm(data=serializer.validated_data, request=request)
        if not form.is_valid():
            return Response(form.errors, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        with transaction.atomic():
            case = form.save()
            case.contributors.add(request.user)

            # Create entity binds (NES ids) for alleged/related entities
            for nes_id in serializer.validated_data.get("alleged_entities", []):
                CaseEntityRelationship.objects.get_or_create(
                    case=case,
                    nes_id=nes_id,
                    relationship_type=RelationshipType.ACCUSED,
                )
            for nes_id in serializer.validated_data.get("related_entities", []):
                CaseEntityRelationship.objects.get_or_create(
                    case=case,
                    nes_id=nes_id,
                    relationship_type=RelationshipType.RELATED,
                )

        return Response(CaseSerializer(case).data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, *args, **kwargs):
        """
        GET /api/cases/{id}/

        Retrieve a case with permission-based access control:
        - PUBLISHED cases: accessible to everyone (public)
        - DRAFT and IN_REVIEW cases (casework): require a casework-viewing role
          (readonly/caseworker/moderator/admin); public callers get 404
        - CLOSED cases: not accessible via public API (returns 404)
        """
        case = self.get_object()

        # Casework states (DRAFT, IN_REVIEW) are not public — require authz.
        # Public = readonly EXCEPT no casework access, so it cannot view these.
        if case.state in (CaseState.DRAFT, CaseState.IN_REVIEW):
            if not request.user.is_authenticated:
                return Response(
                    {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
                )

            # Check if user is authorized to view this casework case.
            if not can_view_case(request.user, case):
                return Response(
                    {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
                )

        # Case is accessible - return serialized data
        serializer = self.get_serializer(case)
        return Response(serializer.data)

    def partial_update(self, request, *args, **kwargs):
        """
        PATCH /api/cases/{id}/

        Accepts an RFC 6902 JSON Patch document and applies it against a writable
        snapshot of the case. The snapshot is validated after patching, then scalar
        fields are saved via a bulk UPDATE and M2M relations are updated with .set().

        Blocked paths (id, version, contributors, timestamps,
        versionInfo) are rejected before the patch is applied.
        """
        # get_object() raises DRF's Http404/NotFound (→ 404) when the case is
        # absent; the ViewSet's queryset already scopes visibility, so no manual
        # DoesNotExist handling is needed here.
        case = self.get_object()

        if not can_change_case(request.user, case):
            return Response(
                {"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN
            )

        patch_ops = request.data
        if not isinstance(patch_ops, list):
            return Response(
                {"detail": "Request body must be a JSON array of patch operations."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Reject blocked paths before applying the patch
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
        # Note: only IN_REVIEW transitions are supported by this endpoint today.
        # Admins/moderators may be allowed other transitions in future PRs.
        # Non-IN_REVIEW targets will be rejected with 422 below even if the
        # permission check above passes.

        # Gate entity rewrite to actual /entities patch ops.
        # _build_snapshot always includes "entities" in the snapshot, so
        # "entities" will always be present in validated after apply_patch.
        # Checking validated alone would wipe relationships on every PATCH.
        entities_touched = any(
            isinstance(op, dict)
            and (
                op.get("path") == "/entities"
                or op.get("path", "").startswith("/entities/")
            )
            for op in patch_ops
        )

        # Fields that map directly to Case model columns (updated via bulk UPDATE)
        scalar_fields = frozenset(
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
                "evidence",
                "slug",
                "court_cases",
                "missing_details",
                "bigo",
            ]
        )

        with transaction.atomic():
            # Persist scalar field changes
            scalar_updates = {
                field: validated[field] for field in scalar_fields if field in validated
            }
            if scalar_updates:
                case = self.get_object()
                Case.objects.filter(pk=case.pk).update(**scalar_updates)

            # Persist entity relationship changes only when a /entities op
            # was explicitly included — avoids unnecessary delete/recreate on
            # scalar-only PATCHes.
            case.refresh_from_db()
            if entities_touched:
                case.entity_relationships.all().delete()
                for item in validated["entities"]:
                    CaseEntityRelationship.objects.create(
                        case=case,
                        nes_id=item["nes_id"],
                        relationship_type=item["relationship_type"],
                        notes=item.get("notes") or "",
                    )

            case.refresh_from_db()

            if target_state is not None and target_state != case.state:
                if target_state == CaseState.IN_REVIEW:
                    try:
                        case.submit()
                    except ValidationError as exc:
                        detail = getattr(exc, "message_dict", None) or {
                            "detail": exc.messages
                        }
                        transaction.set_rollback(True)
                        return Response(detail, status=status.HTTP_400_BAD_REQUEST)
                else:
                    transaction.set_rollback(True)
                    return Response(
                        {
                            "detail": "Only transitions to IN_REVIEW are supported via this endpoint."
                        },
                        status=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    )

        # Scalar edits go through queryset .update() and entity-relationship
        # edits through bulk delete/create — neither fires post_save, so the
        # live search-index signal never runs. Re-index explicitly (best-effort,
        # on_commit) so a PUBLISHED case's content/slug/relationships stay fresh
        # in the index; a non-PUBLISHED case is evicted by the same call.
        from .search_index import index as _index_case

        transaction.on_commit(lambda: _index_case(case))

        return Response(CaseSerializer(case).data, status=status.HTTP_200_OK)

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

        # Case.delete() is overridden to soft-delete (state -> CLOSED + versionInfo
        # audit entry); it never hard-removes the row. The post-transition CLOSED
        # state is evicted from the search index by the case save signal.
        case.delete()
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
            "evidence": list(case.evidence) if case.evidence else [],
            "entities": [
                {
                    "nes_id": rel.nes_id,
                    "relationship_type": rel.relationship_type,
                    "notes": rel.notes or "",
                }
                for rel in case.entity_relationships.all()
            ],
            "slug": case.slug,
            "court_cases": list(case.court_cases) if case.court_cases else [],
            "missing_details": case.missing_details,
            "bigo": case.bigo,
        }


@extend_schema_view(
    list=extend_schema(
        summary="List document sources",
        description="""
        Retrieve a paginated list of document sources.

        Sources associated with published or in-review cases are accessible to
        all callers. Users in the org-wide ReadOnly role get a system-wide read:
        every non-deleted source, including those referenced only by DRAFT cases
        or not referenced by any case. Soft-deleted sources (is_deleted=True) are
        always excluded.

        **Pagination:**
        - Results are paginated with 20 items per page
        - Use `page` parameter to navigate pages
        """,
        parameters=[
            OpenApiParameter(
                name="page",
                type=OpenApiTypes.INT,
                location=OpenApiParameter.QUERY,
                description="Page number for pagination",
                required=False,
            ),
        ],
        tags=["sources"],
    ),
    retrieve=extend_schema(
        summary="Retrieve a document source",
        description="""
        Retrieve detailed information about a specific document source.

        The endpoint accepts either the database id (numeric) or the source_id
        (e.g., 'source:20240115:abc123').

        Only sources associated with at least one published or in-review case are accessible.
        """,
        tags=["sources"],
    ),
    create=extend_schema(
        summary="Create a new document source",
        description="""
        Create a new document source with an optional file upload.

        Requires authentication and the `cases.add_documentsource` permission.
        Accepts multipart form data.
        """,
        tags=["sources"],
    ),
)
class DocumentSourceViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """
    Public API for DocumentSources.

    Provides:
    - List endpoint: GET /api/sources/
    - Retrieve endpoint: GET /api/sources/{id_or_source_id}/
    - Create endpoint: POST /api/sources/
    - Update endpoint: PATCH/PUT /api/sources/{id_or_source_id}/

    The retrieve/update endpoints accept either the database id or the source_id.
    Sources tied to published or in-review cases are accessible to all callers;
    the org-wide ReadOnly role additionally reads every non-deleted source.
    Create requires cases.add_documentsource; update requires
    cases.change_documentsource (enforced via DjangoModelPermissions).
    """

    parser_classes = [MultiPartParser, FormParser, JSONParser]
    lookup_field = "pk"

    def get_permissions(self):
        # Writes require the matching Django model permission (DjangoModelPermissions
        # maps POST->add_documentsource, PUT/PATCH->change_documentsource) on top of
        # authentication. This keeps the org-wide ReadOnly role (view-only perms) and
        # plain authenticated users without source perms out of the write paths.
        if self.action in ("create", "partial_update", "update", "destroy"):
            return [IsAuthenticated(), DjangoModelPermissions()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == "create":
            from .serializers import DocumentSourceCreateSerializer

            return DocumentSourceCreateSerializer
        if self.action in ("partial_update", "update"):
            from .serializers import DocumentSourceUpdateSerializer

            return DocumentSourceUpdateSerializer
        return DocumentSourceSerializer

    def update(self, request, *args, **kwargs):
        kwargs["partial"] = True  # treat PUT as PATCH (partial updates only)
        partial = kwargs.pop("partial", True)
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        read_serializer = DocumentSourceSerializer(
            instance, context=self.get_serializer_context()
        )
        return Response(read_serializer.data)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        # Return response using the read serializer
        read_serializer = DocumentSourceSerializer(
            serializer.instance, context=self.get_serializer_context()
        )
        return Response(read_serializer.data, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        """
        DELETE /api/sources/{id_or_source_id}/

        Soft-delete a document source by setting ``is_deleted=True`` (the model
        already carries this flag + an admin soft-delete action). The row is
        preserved for audit history and excluded from all reads. Returns 204.
        Requires the cases.delete_documentsource permission (get_permissions()).
        """
        instance = self.get_object()
        instance.is_deleted = True
        instance.save(update_fields=["is_deleted", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

    def get_queryset(self):
        """
        Return only sources referenced in evidence of published or in-review cases.

        A source is accessible if it's referenced in the evidence field
        of at least one published or in-review case.
        """
        # On create, DjangoModelPermissions calls get_queryset() solely to derive
        # the model for the permission check. Short-circuit so the expensive
        # visibility scan (every published/in-review case + JSON evidence parse)
        # does not run on the POST hot path. .none() still carries .model.
        # (update/partial_update legitimately need the real queryset via
        # get_object(), so they fall through.)
        if self.action == "create":
            return DocumentSource.objects.none()

        # DELETE addresses any non-deleted source (the delete_documentsource model
        # permission is the gate); get_object() resolves it by id or source_id.
        if self.action == "destroy":
            return DocumentSource.objects.filter(is_deleted=False).distinct()

        # The org-wide ReadOnly role gets a system-wide read: all non-deleted
        # sources, including those referenced only by DRAFT cases (or by no case
        # at all). Other callers keep the public contract below.
        user = self.request.user
        if user and user.is_authenticated and is_readonly(user):
            return DocumentSource.objects.filter(is_deleted=False).distinct()

        allowed_states = [CaseState.PUBLISHED, CaseState.IN_REVIEW]
        visible_cases = Case.objects.filter(state__in=allowed_states)

        # Extract all source_ids from evidence fields
        source_ids = set()
        for case in visible_cases:
            if case.evidence:
                for evidence_item in case.evidence:
                    if isinstance(evidence_item, dict) and "source_id" in evidence_item:
                        source_ids.add(evidence_item["source_id"])

        # Return sources that are referenced and not soft-deleted
        return DocumentSource.objects.filter(
            source_id__in=source_ids, is_deleted=False
        ).distinct()

    def get_object(self):
        """
        Override to support lookup by either id or source_id.

        Tries to lookup by id first (if numeric), then falls back to source_id.
        """
        queryset = self.filter_queryset(self.get_queryset())
        lookup_value = self.kwargs.get(self.lookup_field)

        # Try to lookup by id if the value is numeric
        if lookup_value.isdigit():
            try:
                obj = queryset.get(id=int(lookup_value))
                self.check_object_permissions(self.request, obj)
                return obj
            except DocumentSource.DoesNotExist:
                pass

        # Fall back to lookup by source_id
        try:
            obj = queryset.get(source_id=lookup_value)
            self.check_object_permissions(self.request, obj)
            return obj
        except DocumentSource.DoesNotExist:
            from rest_framework.exceptions import NotFound

            raise NotFound(f"Source with id or source_id '{lookup_value}' not found.")


def _pct(part: int, whole: int) -> float:
    """Percentage of ``part`` over ``whole``, 1 dp; 0.0 when ``whole`` is 0."""
    return round((part / whole) * 100, 1) if whole else 0.0


@extend_schema(
    summary="Get case statistics",
    description="""
    Retrieve aggregate statistics about cases in the system.

    Returns:
    - `published_cases`: Number of cases with state PUBLISHED
    - `cases_under_investigation`: Number of cases with state DRAFT or IN_REVIEW
    - `cases_closed`: Number of cases with state CLOSED
    - `entities_tracked`: Number of unique entities involved in published cases
    - `nes`: NES (entities) coverage — total, by-prefix / by-type breakdowns, and
      completeness percentages (identifier / provenance / bilingual name)
    - `ngm`: NGM (judicial) coverage — court-case / court totals, by-court-type
      breakdown, and completeness percentages (NES-resolved / registration date /
      document sources)
    - `materials`: NGM materials (development-project / document dataset) coverage —
      total, by-type / by-source breakdowns, and completeness percentages
      (description / url / date)
    - `last_updated`: Timestamp when statistics were last calculated

    **Caching:**
    - Statistics are cached for 5 minutes to optimize performance
    - The cache is automatically refreshed after expiration
    """,
    tags=["statistics"],
    responses={
        200: {
            "type": "object",
            "properties": {
                "published_cases": {"type": "integer", "example": 127},
                "cases_under_investigation": {"type": "integer", "example": 43},
                "cases_closed": {"type": "integer", "example": 31},
                "entities_tracked": {"type": "integer", "example": 89},
                "nes": {"type": "object", "description": "NES coverage metrics"},
                "ngm": {"type": "object", "description": "NGM judicial coverage metrics"},
                "materials": {"type": "object", "description": "NGM materials coverage metrics"},
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

    Provides aggregate counts of cases by state and unique entities tracked.
    Results are cached for 5 minutes using LocMemCache.
    """

    def get(self, request):
        """
        Get cached or calculate fresh statistics.
        """
        cache_key = "stats-cache"

        # Try to get from cache
        cached_data = cache.get(cache_key)
        if cached_data:
            return Response(cached_data)

        # Calculate statistics
        stats = {
            "published_cases": Case.objects.filter(state=CaseState.PUBLISHED).count(),
            "cases_under_investigation": Case.objects.filter(
                state__in=[CaseState.DRAFT, CaseState.IN_REVIEW]
            ).count(),
            "cases_closed": Case.objects.filter(state=CaseState.CLOSED).count(),
            # Unique NES entities tracked across published cases (binds hold the
            # nes_id directly; NES owns the entity records).
            "entities_tracked": (
                CaseEntityRelationship.objects.filter(
                    case__state=CaseState.PUBLISHED
                )
                .values("nes_id")
                .distinct()
                .count()
            ),
            # Cross-source coverage for the Data Quality dashboard. The DB router
            # sends each model below to its own database (nes / ngm).
            "nes": self._nes_metrics(),
            "ngm": self._ngm_metrics(),
            "materials": self._materials_metrics(),
            "last_updated": timezone.now().isoformat(),
        }

        # Cache for 5 minutes
        cache.set(cache_key, stats, timeout=300)

        return Response(stats)

    @staticmethod
    def _nes_metrics():
        """NES (entities) coverage — totals, breakdowns, completeness.

        Counts are server-side aggregates over indexed promoted columns. The
        completeness signals live inside the ``data`` JSON-LD column; on Postgres
        they are answered with JSON-key existence lookups, on sqlite (the empty
        local stores / test DB) they degrade to 0 without a full-table scan.
        """
        total = StoredEntity.objects.count()

        by_prefix = list(
            StoredEntity.objects.values("prefix")
            .annotate(count=Count("iri"))
            .order_by("-count")
        )
        by_type = list(
            StoredEntity.objects.values("entity_type")
            .annotate(count=Count("iri"))
            .order_by("-count")
        )

        # Completeness signals reflect what is ACTUALLY stored on the entity JSON-LD
        # doc. NOTE: source attributions are NOT carried on the published doc (they
        # live in the bulk-ingest envelope + the version provenance), so we measure
        # the real stored provenance instead: ``identifier`` (the stable external id
        # — ECN candidate-id / pcode / reg-no, present on ~all sourced entities) and
        # ``jawafdehi:version`` (the authored version/provenance block). ``name``
        # bilingualism is measured directly. (An earlier draft keyed on
        # ``jawafdehi:sources`` / ``description``, which no entity carries → always 0.)
        if connection.vendor == "postgresql":
            with_identifier = StoredEntity.objects.filter(
                data__has_key="identifier"
            ).count()
            with_provenance = StoredEntity.objects.filter(
                data__has_key="jawafdehi:version"
            ).count()
            with_bilingual_name = (
                StoredEntity.objects.filter(data__name__has_key="en")
                .filter(data__name__has_key="ne")
                .count()
            )
        else:
            with_identifier = with_provenance = with_bilingual_name = 0

        return {
            "total": total,
            "by_prefix": by_prefix,
            "by_type": by_type,
            "counts": {
                "with_identifier": with_identifier,
                "with_provenance": with_provenance,
                "with_bilingual_name": with_bilingual_name,
            },
            "completeness": {
                "with_identifier": _pct(with_identifier, total),
                "with_provenance": _pct(with_provenance, total),
                "with_bilingual_name": _pct(with_bilingual_name, total),
            },
        }

    @staticmethod
    def _ngm_metrics():
        """NGM (judicial) coverage — court-case / court totals, breakdowns, and
        completeness over court cases (all indexed columns). Materials are a
        distinct dataset with their own block — see ``_materials_metrics``."""
        court_cases_total = CourtCase.objects.count()

        by_court_type = list(
            CourtCase.objects.values("court__court_type")
            .annotate(count=Count("case_number"))
            .order_by("-count")
        )

        nes_resolved = (
            CourtCase.objects.exclude(nes_id__isnull=True)
            .exclude(nes_id="")
            .count()
        )
        with_registration_date = CourtCase.objects.exclude(
            registration_date_ad__isnull=True
        ).count()
        if connection.vendor == "postgresql":
            with_document_sources = (
                CourtCase.objects.filter(document_sources__isnull=False)
                .exclude(document_sources=[])
                .count()
            )
        else:
            with_document_sources = CourtCase.objects.exclude(
                document_sources__isnull=True
            ).count()

        return {
            "court_cases_total": court_cases_total,
            "courts_total": Court.objects.count(),
            "by_court_type": by_court_type,
            "counts": {
                "nes_resolved": nes_resolved,
                "with_registration_date": with_registration_date,
                "with_document_sources": with_document_sources,
            },
            "completeness": {
                "nes_resolved": _pct(nes_resolved, court_cases_total),
                "with_registration_date": _pct(
                    with_registration_date, court_cases_total
                ),
                "with_document_sources": _pct(
                    with_document_sources, court_cases_total
                ),
            },
        }

    @staticmethod
    def _materials_metrics():
        """Materials (NGM development-project / document dataset) coverage —
        total, by-type / by-source breakdowns, and completeness measured over
        the material ``data`` JSON-LD doc. Materials are NOT judicial records;
        they get their own block separate from ``_ngm_metrics``."""
        total = Material.objects.count()

        by_type = list(
            Material.objects.values("material_type")
            .annotate(count=Count("iri"))
            .order_by("-count")
        )
        by_source = list(
            Material.objects.values("source")
            .annotate(count=Count("iri"))
            .order_by("-count")
        )

        # Completeness signals over the stored schema.org JSON-LD doc. On Postgres
        # answered with JSON-key existence lookups; sqlite (empty local / test DB)
        # degrades to 0 without a full scan.
        if connection.vendor == "postgresql":
            with_description = Material.objects.filter(
                data__has_key="description"
            ).count()
            with_url = Material.objects.filter(data__has_key="url").count()
            with_date = Material.objects.filter(
                data__has_key="dateCreated"
            ).count()
        else:
            with_description = with_url = with_date = 0

        return {
            "total": total,
            "by_type": by_type,
            "by_source": by_source,
            "counts": {
                "with_description": with_description,
                "with_url": with_url,
                "with_date": with_date,
            },
            "completeness": {
                "with_description": _pct(with_description, total),
                "with_url": _pct(with_url, total),
                "with_date": _pct(with_date, total),
            },
        }


class FeedbackRateThrottle(AnonRateThrottle):
    """Rate throttle for feedback submissions: 5 per hour."""

    rate = "5/hour"


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
            # Capture metadata
            feedback = serializer.save(
                ip_address=self.get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )

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


OEMBED_CASE_URL_PATTERN = re.compile(
    r"^https?://(?:www\.)?jawafdehi\.org/case/(?P<slug>[^/?#]+)"
)
EMBED_BASE_URL = "https://jawafdehi.org"
DEFAULT_EMBED_WIDTH = 600
DEFAULT_EMBED_HEIGHT = 300


@extend_schema(
    summary="oEmbed endpoint",
    description="""
    oEmbed provider endpoint for Jawafdehi case pages.

    When a journalist pastes a Jawafdehi case URL into Substack, Medium,
    WordPress, or any oEmbed-compatible platform, the platform discovers
    this endpoint and requests an embeddable widget.

    **Parameters:**
    - `url` (required): the jawafdehi.org case URL to embed
    - `format` (optional): response format — `json` (default) or `xml`

    Only published cases are available for embedding.
    Returns a `rich` type embed with an iframe pointing to the embed card.
    """,
    parameters=[
        OpenApiParameter(
            name="url",
            type=OpenApiTypes.URI,
            location=OpenApiParameter.QUERY,
            description="Full jawafdehi.org/case/{slug} URL to embed",
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

    Extracts the case slug from the provided URL, looks up the published case,
    and returns an oEmbed response with an iframe embed code.
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

        match = OEMBED_CASE_URL_PATTERN.match(url)
        if not match:
            return Response(
                {
                    "error": (
                        "URL does not match a supported Jawafdehi case pattern. "
                        "Expected: https://jawafdehi.org/case/{slug}"
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        slug = match.group("slug")

        try:
            case = Case.objects.get(slug=slug, state=CaseState.PUBLISHED)
        except Case.DoesNotExist:
            return Response(
                {"error": "Case not found or not published."},
                status=status.HTTP_404_NOT_FOUND,
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

        embed_url = f"{EMBED_BASE_URL}/embed/case/{slug}"

        oembed_data = {
            "type": "rich",
            "version": "1.0",
            "title": case.title,
            "author_name": "Jawafdehi Editorial",
            "author_url": EMBED_BASE_URL,
            "provider_name": "Jawafdehi",
            "provider_url": EMBED_BASE_URL,
            "cache_age": 3600,
            "html": (
                f'<iframe src="{embed_url}" '
                f'width="{width}" '
                f'height="{height}" '
                f'frameborder="0" '
                f'allowtransparency="true" '
                f'scrolling="no" '
                f'style="border:0;overflow:hidden;max-width:100%;" '
                f'title="{case.title}">'
                f"</iframe>"
            ),
            "width": width,
            "height": height,
            "thumbnail_url": case.thumbnail_url or "",
            "thumbnail_width": width if case.thumbnail_url else None,
            "thumbnail_height": height if case.thumbnail_url else None,
        }

        if response_format == "xml":
            return self._xml_response(oembed_data)

        return Response(oembed_data)

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
                "user_id": real_user.id,
                "username": real_user.get_username(),
                "owui_user_id": identity.owui_user_id,
                "owui_user_name": identity.owui_user_name,
            }
        )
