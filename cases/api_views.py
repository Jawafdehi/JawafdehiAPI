"""
API ViewSets for the Jawafdehi accountability platform.

See: .kiro/specs/accountability-platform-core/design.md
"""

import logging
import jsonpatch
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.http import Http404
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    extend_schema,
    extend_schema_view,
)
from django.db.models import Q
from rest_framework import filters, mixins, status, viewsets
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.authentication import JWTAuthentication

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
    JawafEntity,
    RelationshipType,
)
from .rules.predicates import (
    can_change_case,
    can_transition_case_state,
    can_view_case,
    is_admin_or_moderator,
)
from .serializers import (
    CaseDetailSerializer,
    CaseSerializer,
    DocumentSourceSerializer,
    FeedbackSerializer,
    JawafEntitySerializer,
)

logger = logging.getLogger(__name__)


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
        - Admin / Moderator: all non-CLOSED cases (PUBLISHED + IN_REVIEW + DRAFT).
        - Authenticated contributor/other: PUBLISHED cases + any DRAFT or IN_REVIEW cases
          they are explicitly assigned to as contributors.

        Results are ordered by creation date (newest first).
        
        **Filtering:**
        - `case_type`: Filter by case type (CORRUPTION or PROMISES)
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
                enum=["CORRUPTION", "PROMISES"],
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
        - PUBLISHED and IN_REVIEW cases: accessible to everyone
        - DRAFT cases: require authorization (admins, moderators, or assigned contributors)
        - CLOSED cases: not accessible via public API
        
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
    - Create endpoint: POST /api/cases/ (authenticated users only)
    - List endpoint: GET /api/cases/
    - Retrieve endpoint: GET /api/cases/{id}/
    - Patch endpoint: PATCH /api/cases/{id}/ (authenticated users only)

    Filtering:
    - case_type: Filter by case type
    - tags: Filter by tags

    Search:
    - Full-text search across title, description, key_allegations

    Only published cases (state=PUBLISHED) are accessible.
    The detail endpoint also includes IN_REVIEW cases.
    """

    serializer_class = CaseSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["case_type"]
    search_fields = ["title", "description", "key_allegations"]
    authentication_classes = [
        JWTAuthentication,
        TokenAuthentication,
        SessionAuthentication,
    ]

    def get_permissions(self):
        if self.action in ("create", "partial_update"):
            return [IsAuthenticated()]
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

        List endpoint: PUBLISHED cases only (regardless of authorization).
        Retrieve endpoint:
          - Unauthenticated: PUBLISHED and IN_REVIEW cases
          - Authenticated: PUBLISHED, IN_REVIEW, and DRAFT cases (authorization check in retrieve)
          - CLOSED cases are never exposed via public API
        Partial update endpoint: all cases except CLOSED (authorization check happens in partial_update).
        """
        if self.action == "partial_update":
            # PATCH endpoint: return all cases except CLOSED, authorization check happens in partial_update method
            return Case.objects.exclude(state=CaseState.CLOSED)

        if self.action == "retrieve":
            # Authenticated users can potentially access DRAFT cases (authorization check in retrieve)
            if self.request.user and self.request.user.is_authenticated:
                # Exclude CLOSED cases from public API
                queryset = Case.objects.exclude(state=CaseState.CLOSED)
            else:
                # Unauthenticated users: only PUBLISHED and IN_REVIEW
                queryset = Case.objects.filter(
                    state__in=[CaseState.PUBLISHED, CaseState.IN_REVIEW]
                )
        else:
            # List endpoint: visibility depends on authentication/role.
            # - Unauthenticated: PUBLISHED only
            # - Admin/Moderator: all non-CLOSED cases
            # - Other authenticated (contributor): PUBLISHED + cases they are assigned to
            if not (self.request.user and self.request.user.is_authenticated):
                queryset = Case.objects.filter(state=CaseState.PUBLISHED)
            elif is_admin_or_moderator(self.request.user):
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
            "entity_relationships__entity",
        ).order_by("-created_at")

    def get_object(self):
        """
        Override to support lookup by either numeric id or slug.

        Lookup disambiguation:
        - If lookup_value is purely numeric → query by id
        - Otherwise → query by slug

        Deprecation tracking:
        - Numeric ID lookups trigger deprecation warning
        - Sets request._used_numeric_id flag for response header

        Returns:
            Case: The retrieved case object

        Raises:
            Http404: If no case matches the lookup value
        """
        queryset = self.filter_queryset(self.get_queryset())
        lookup_value = self.kwargs.get(self.lookup_field)

        # Disambiguation logic
        if lookup_value.isdigit():
            # Numeric ID lookup (deprecated)
            try:
                obj = queryset.get(id=int(lookup_value))
                self.check_object_permissions(self.request, obj)

                # Track deprecation
                self.request._used_numeric_id = True

                return obj
            except (Case.DoesNotExist, ValueError) as exc:
                raise Http404("Not found.") from exc
        else:
            # Slug lookup (preferred)
            try:
                obj = queryset.get(slug=lookup_value)
                self.check_object_permissions(self.request, obj)
                return obj
            except Case.DoesNotExist as exc:
                raise Http404("Not found.") from exc

    def finalize_response(self, request, response, *args, **kwargs):
        """
        Override to add deprecation header for numeric ID lookups.

        Checks for _used_numeric_id flag set by get_object() and adds
        the Deprecation header if present.
        """
        response = super().finalize_response(request, response, *args, **kwargs)

        # Add deprecation header if numeric ID was used
        if getattr(request, "_used_numeric_id", False):
            response["Deprecation"] = "true"

        return response

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
        form.fields["case_id"].required = False
        if not form.is_valid():
            return Response(form.errors, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        with transaction.atomic():
            case = form.save()
            case.contributors.add(request.user)

            # Create entity relationships for alleged/related entities
            for entity_id in serializer.validated_data.get("alleged_entities", []):
                CaseEntityRelationship.objects.get_or_create(
                    case=case,
                    entity_id=entity_id,
                    relationship_type=RelationshipType.ACCUSED,
                )
            for entity_id in serializer.validated_data.get("related_entities", []):
                CaseEntityRelationship.objects.get_or_create(
                    case=case,
                    entity_id=entity_id,
                    relationship_type=RelationshipType.RELATED,
                )

        return Response(CaseSerializer(case).data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, *args, **kwargs):
        """
        GET /api/cases/{id}/

        Retrieve a case with permission-based access control:
        - PUBLISHED and IN_REVIEW cases: accessible to everyone
        - DRAFT cases: require authorization (user must have permission to view)
        - CLOSED cases: not accessible via public API (returns 404)
        """
        case = self.get_object()

        # Check if case requires authorization
        if case.state == CaseState.DRAFT:
            # DRAFT cases require authorization
            if not request.user.is_authenticated:
                return Response(
                    {"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND
                )

            # Check if user is authorized to view this case
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

        Blocked paths (id, case_id, version, contributors, timestamps,
        versionInfo) are rejected before the patch is applied.
        """
        try:
            case = self.get_object()
        except Case.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

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
                        entity_id=item["entity"],
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

        return Response(CaseSerializer(case).data, status=status.HTTP_200_OK)

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
                    "entity": rel.entity_id,
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
        
        Only sources associated with published or in-review cases are accessible.
        Soft-deleted sources (is_deleted=True) are excluded.
        
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
        
        Requires authentication. Accepts multipart form data.
        """,
        tags=["sources"],
    ),
)
class DocumentSourceViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """
    Public API for DocumentSources.

    Provides:
    - List endpoint: GET /api/sources/
    - Retrieve endpoint: GET /api/sources/{id_or_source_id}/
    - Create endpoint: POST /api/sources/

    The retrieve endpoint accepts either the database id or the source_id.
    Only sources associated with published or in-review cases are accessible.
    """

    parser_classes = [MultiPartParser, FormParser, JSONParser]
    lookup_field = "pk"

    def get_permissions(self):
        if self.action == "create":
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == "create":
            from .serializers import DocumentSourceCreateSerializer

            return DocumentSourceCreateSerializer
        return DocumentSourceSerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        # Return response using the read serializer
        read_serializer = DocumentSourceSerializer(
            serializer.instance, context=self.get_serializer_context()
        )
        return Response(read_serializer.data, status=status.HTTP_201_CREATED)

    def get_queryset(self):
        """
        Return only sources referenced in evidence of published or in-review cases.

        A source is accessible if it's referenced in the evidence field
        of at least one published or in-review case.
        """
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
        return (
            DocumentSource.objects.filter(source_id__in=source_ids, is_deleted=False)
            .prefetch_related("uploaded_files")
            .distinct()
        )

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


@extend_schema_view(
    list=extend_schema(
        summary="List all entities",
        description="""
        Retrieve a paginated list of all entities in the system.
        
        Entities can have either:
        - `nes_id`: Reference to Nepal Entity Service
        - `display_name`: Custom entity name
        - Both fields (display_name is optional when nes_id is present)
        
        **Search:**
        - `search`: Search across nes_id and display_name
        
        **Pagination:**
        - Results are paginated with 50 items per page
        - Use `page` parameter to navigate pages
        """,
        parameters=[
            OpenApiParameter(
                name="search",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                description="Search across nes_id and display_name",
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
        tags=["entities"],
    ),
    retrieve=extend_schema(
        summary="Retrieve an entity",
        description="""
        Retrieve detailed information about a specific entity.
        
        Returns entity with id, nes_id, and display_name.
        """,
        tags=["entities"],
    ),
    create=extend_schema(
        summary="Create an entity",
        description="""
        Create a new JawafEntity.
        
        Requires authentication.
        """,
        tags=["entities"],
    ),
)
class JawafEntityViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """
    Public API for JawafEntities.

    Provides:
    - List endpoint: GET /api/entities/ (filtered by case association)
    - Retrieve endpoint: GET /api/entities/{id}/
    - Create endpoint: POST /api/entities/

    Search:
    - Full-text search across nes_id and display_name

    Only entities associated with published cases are returned in list view.
    Entities must appear in alleged_entities or related_entities (not locations).
    """

    filter_backends = [filters.SearchFilter]
    search_fields = ["nes_id", "display_name"]

    def get_permissions(self):
        if self.action == "create":
            return [IsAuthenticated()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == "create":
            from .serializers import JawafEntityCreateSerializer

            return JawafEntityCreateSerializer
        return JawafEntitySerializer

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)

        # Return response using the read serializer
        read_serializer = JawafEntitySerializer(
            serializer.instance, context=self.get_serializer_context()
        )
        return Response(read_serializer.data, status=status.HTTP_201_CREATED)

    def get_queryset(self):
        """
        Return entities based on action.

        For list action: Only entities that appear in published cases.
        For retrieve action: All entities (no filtering).

        An entity is included in list if it appears in alleged_entities or
        related_entities of at least one published case.

        Note: Location entities are excluded from the list.

        Uses caching to avoid expensive queryset evaluation.
        """
        # For retrieve action, return all entities
        if self.action == "retrieve":
            return JawafEntity.objects.all()

        # For list action, filter by case association
        from django.core.cache import cache

        # Try to get entity IDs from cache
        entity_ids = cache.get("public_entities_list")

        if entity_ids is None:
            # Cache miss - compute entity IDs
            published_cases = Case.objects.filter(state=CaseState.PUBLISHED)

            entity_ids = set()
            for case in published_cases:
                # Add all entities from unified entities format, excluding locations
                entity_ids.update(
                    case.unified_entities.exclude(
                        nes_id__startswith="entity:location/"
                    ).values_list("id", flat=True)
                )

            # Cache for 10 minutes - stale cache is acceptable
            cache.set("public_entities_list", entity_ids, timeout=600)

        return JawafEntity.objects.filter(id__in=entity_ids).order_by("-created_at")


@extend_schema(
    summary="Get case statistics",
    description="""
    Retrieve aggregate statistics about cases in the system.
    
    Returns:
    - `published_cases`: Number of cases with state PUBLISHED
    - `cases_under_investigation`: Number of cases with state DRAFT or IN_REVIEW
    - `cases_closed`: Number of cases with state CLOSED
    - `entities_tracked`: Number of unique entities involved in published cases
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
            "entities_tracked": JawafEntity.objects.count(),
            "last_updated": timezone.now().isoformat(),
        }

        # Cache for 5 minutes
        cache.set(cache_key, stats, timeout=300)

        return Response(stats)


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
