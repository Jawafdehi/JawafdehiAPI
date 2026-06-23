"""
API ViewSets for the Jawafdehi accountability platform.

See: .kiro/specs/accountability-platform-core/design.md
"""

import logging
import re
from xml.etree.ElementTree import Element, SubElement, tostring

import jsonpatch
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q
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
from rest_framework import filters, mixins, serializers, status, viewsets
from rest_framework.authentication import SessionAuthentication, TokenAuthentication
from rest_framework.exceptions import NotFound
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import DjangoModelPermissions, IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from config.oidc import OIDCJWTAuthentication
from ngm.services import normalize_case_number

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
    CaseType,
    DocumentSource,
    JawafEntity,
    RelationshipType,
)
from .rules.predicates import (
    can_change_case,
    can_transition_case_state,
    can_view_case,
    is_admin_or_moderator,
    is_contributor,
    is_readonly,
)
from .search_serializers import SearchResponseSerializer
from .serializers import (
    CaseDetailSerializer,
    CaseListSerializer,
    CaseSerializer,
    DocumentSourceSerializer,
    FeedbackSerializer,
    JawafEntitySerializer,
)
from .services.search import UnifiedSearchService
from .throttles import AnonRateThrottle, get_client_ident

logger = logging.getLogger(__name__)


class UnifiedSearchQuerySerializer(serializers.Serializer):
    q = serializers.CharField(required=False, allow_blank=True, default="")
    type = serializers.ListField(
        child=serializers.ChoiceField(choices=["case", "entity", "document"]),
        required=False,
        default=list,
    )
    entity_type = serializers.ListField(
        child=serializers.ChoiceField(
            choices=["person", "organization", "location", "unknown"]
        ),
        required=False,
        default=list,
    )
    role = serializers.ListField(
        child=serializers.ChoiceField(choices=RelationshipType.values),
        required=False,
        default=list,
    )
    case_type = serializers.ListField(
        child=serializers.ChoiceField(choices=CaseType.values),
        required=False,
        default=list,
    )
    tags = serializers.ListField(
        child=serializers.CharField(allow_blank=False),
        required=False,
        default=list,
    )
    sort = serializers.ChoiceField(
        choices=["relevance", "newest", "oldest", "title"],
        required=False,
        default="relevance",
    )
    page = serializers.IntegerField(required=False, min_value=1, default=1)
    page_size = serializers.IntegerField(required=False, min_value=1, default=10)


@extend_schema(
    summary="Search the accountability archive",
    description="""
    Search published accountability cases, entities, and evidence documents in
    one deterministic relevance-ranked result list. Sidebar filters accept
    repeated query parameters, including record type. Entity types are derived
    locally from NES IDs; this endpoint does not call external services.
    """,
    parameters=[
        OpenApiParameter("q", OpenApiTypes.STR, OpenApiParameter.QUERY, required=False),
        OpenApiParameter(
            "type",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            enum=["case", "entity", "document"],
            many=True,
        ),
        OpenApiParameter(
            "entity_type",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            enum=["person", "organization", "location", "unknown"],
            many=True,
        ),
        OpenApiParameter(
            "role",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            enum=RelationshipType.values,
            many=True,
        ),
        OpenApiParameter(
            "case_type",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            enum=CaseType.values,
            many=True,
        ),
        OpenApiParameter(
            "tags",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            many=True,
        ),
        OpenApiParameter(
            "sort",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            enum=["relevance", "newest", "oldest", "title"],
        ),
        OpenApiParameter(
            "page", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False
        ),
        OpenApiParameter(
            "page_size", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False
        ),
    ],
    responses={200: SearchResponseSerializer},
    tags=["search"],
)
class UnifiedSearchView(APIView):
    """Expose backend-owned mixed archive discovery."""

    def get(self, request):
        serializer = UnifiedSearchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        response = UnifiedSearchService().search(
            request=request, **serializer.validated_data
        )
        return Response(response)


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
        - Admin / Moderator / Contributor / ReadOnly: all non-CLOSED cases
          (PUBLISHED + IN_REVIEW + DRAFT).
        - Other authenticated users: PUBLISHED cases + any DRAFT or IN_REVIEW cases
          they are explicitly assigned to as contributors.

        Results are ordered by creation date (newest first).

        **Filtering:**
        - `case_type`: Filter by case type (e.g. CORRUPTION, BRIBERY, FORGERY)
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
                enum=CaseType.values,
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

        The endpoint accepts either a numeric ID (deprecated), a slug (preferred
        format: kebab-case), or a court case reference of the form
        `{court_identifier}:{case_number}` (e.g. `supreme:081-CR-0081`). The case
        number in a court case reference is normalized like NGM (Devanagari
        digits, casing, zero-padding) and matched against the case's
        `court_cases` list; when several cases cite the same court case, the most
        recently created visible one is returned.

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
                description="Case identifier - numeric ID (deprecated), slug, or court case reference {court_identifier}:{case_number} (e.g. supreme:081-CR-0081)",
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

    Read visibility is role-based: unauthenticated callers see PUBLISHED cases
    (retrieve also exposes IN_REVIEW); Admin / Moderator / Contributor / ReadOnly
    see all non-CLOSED cases (incl. DRAFT). CLOSED cases are never exposed via
    this API.
    """

    serializer_class = CaseSerializer
    lookup_field = "slug"
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["case_type"]
    search_fields = ["title", "description", "key_allegations"]
    authentication_classes = [
        OIDCJWTAuthentication,
        TokenAuthentication,
        SessionAuthentication,
    ]

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
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action == "create":
            return CaseCreateSerializer
        if self.action == "retrieve":
            return CaseDetailSerializer
        if self.action == "list":
            # Slim payload: drops detail-only body fields (description,
            # timeline, evidence, notes, missing_details, versionInfo).
            return CaseListSerializer
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
        if self.action == "create":
            # DjangoModelPermissions calls get_queryset() only to derive the model
            # for the add_case check; return an empty queryset (still carries
            # .model) so the list/tag-filtering path below does not run on POST.
            return Case.objects.none()

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
            # - Admin/Moderator/Contributor/ReadOnly: all non-CLOSED cases
            # - Other authenticated: PUBLISHED + cases they are assigned to
            if not (self.request.user and self.request.user.is_authenticated):
                queryset = Case.objects.filter(state=CaseState.PUBLISHED)
            elif (
                is_admin_or_moderator(self.request.user)
                or is_contributor(self.request.user)
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
            "entity_relationships__entity",
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

        # Re-fetch with the entity prefetch so CaseSerializer.get_entities()
        # reuses the cache instead of firing one query per related entity.
        case = Case.objects.prefetch_related("entity_relationships__entity").get(
            pk=case.pk
        )
        return Response(CaseSerializer(case).data, status=status.HTTP_201_CREATED)

    def get_object(self):
        """
        Resolve the detail lookup value as a slug or a court case reference.

        Slugs never contain a colon, so a lookup value shaped like
        ``{court_identifier}:{case_number}`` (e.g. ``supreme:81-cr-81``) is
        treated as a court case reference: the case number is normalized the
        same way NGM normalizes it and matched against the ``court_cases``
        JSON list. Restricted to retrieve so PATCH keeps its single-target
        slug semantics.
        """
        lookup_value = self.kwargs.get(self.lookup_field) or ""
        if self.action == "retrieve" and ":" in lookup_value:
            return self._get_object_by_court_case(lookup_value)
        return super().get_object()

    def _get_object_by_court_case(self, lookup_value):
        court_identifier, _, raw_case_number = lookup_value.partition(":")
        # court_cases JSON containment is case-sensitive and identifiers are
        # stored lowercase, so normalize the identifier casing too.
        court_identifier = court_identifier.strip().lower()
        raw_case_number = raw_case_number.strip()

        try:
            case_number = normalize_case_number(raw_case_number)
        except ValueError:
            raise NotFound(f"No case found for court case '{lookup_value}'.") from None

        identifier = f"{court_identifier}:{case_number}"
        queryset = self.filter_queryset(self.get_queryset())

        # JSONField containment is only available on PostgreSQL; SQLite (tests)
        # filters in Python, mirroring the tag-filter path in get_queryset().
        if connection.vendor == "postgresql":
            queryset = queryset.filter(court_cases__contains=[identifier])
        else:
            matching_ids = [
                case_id
                for case_id, court_cases in queryset.values_list("id", "court_cases")
                if court_cases and identifier in court_cases
            ]
            queryset = queryset.filter(id__in=matching_ids)

        # Ordered by -created_at in get_queryset(). When several cases cite the
        # same court case, return the most recent one the caller may view, so a
        # newer unviewable DRAFT does not shadow an older viewable case.
        obj = next(
            (
                case
                for case in queryset
                if case.state != CaseState.DRAFT
                or can_view_case(self.request.user, case)
            ),
            None,
        )
        if obj is None:
            raise NotFound(f"No case found for court case '{identifier}'.")

        self.check_object_permissions(self.request, obj)
        return obj

    def retrieve(self, request, *args, **kwargs):
        """
        GET /api/cases/{id}/

        The detail lookup value may be a slug or a court case reference
        (``{court_identifier}:{case_number}``); see get_object().

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
                "internal_notes",
            ]
        )

        with transaction.atomic():
            # Persist scalar field changes
            scalar_updates = {
                field: validated[field] for field in scalar_fields if field in validated
            }
            if scalar_updates:
                case = self.get_object()
                for field, value in scalar_updates.items():
                    setattr(case, field, value)
                # Persist via save(update_fields=...) rather than a bulk
                # queryset .update() so django-auditlog's post_save signal
                # fires and records the change (and the acting user). A bulk
                # .update() bypasses signals, leaving case edits unaudited.
                case.save(update_fields=[*scalar_updates, "updated_at"])

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

        # Re-fetch with the entity prefetch so CaseSerializer.get_entities()
        # reuses the cache instead of firing one query per related entity.
        case = Case.objects.prefetch_related("entity_relationships__entity").get(
            pk=case.pk
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
            "internal_notes": case.internal_notes,
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
        if self.action in ("create", "partial_update", "update"):
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


@extend_schema_view(
    list=extend_schema(
        summary="List entities",
        description="""
        Retrieve a paginated list of entities.

        For most callers the list is limited to entities appearing in published
        cases. Users in the org-wide ReadOnly role get a system-wide read: every
        entity in the system.

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

        Requires authentication and the `cases.add_jawafentity` permission.
        """,
        tags=["entities"],
    ),
)
class JawafEntityViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """
    Public API for JawafEntities.

    Provides:
    - List endpoint: GET /api/entities/ (filtered by case association)
    - Retrieve endpoint: GET /api/entities/{id}/
    - Create endpoint: POST /api/entities/
    - Update endpoint: PATCH /api/entities/{id}/

    Search:
    - Full-text search across nes_id and display_name

    For most callers the list view returns only entities associated with
    published cases (in alleged_entities or related_entities, not locations);
    the org-wide ReadOnly role reads every entity. Create requires
    cases.add_jawafentity; update requires cases.change_jawafentity (enforced
    via DjangoModelPermissions).
    """

    filter_backends = [filters.SearchFilter]
    search_fields = ["nes_id", "display_name"]

    def get_permissions(self):
        # Writes require the matching Django model permission (DjangoModelPermissions
        # maps POST->add_jawafentity, PUT/PATCH->change_jawafentity) on top of
        # authentication, so the org-wide ReadOnly role (view-only perms) and plain
        # authenticated users without entity perms cannot create or edit entities.
        if self.action in ("create", "partial_update", "update"):
            return [IsAuthenticated(), DjangoModelPermissions()]
        return super().get_permissions()

    def get_serializer_class(self):
        if self.action in ("create", "update", "partial_update"):
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
        # On create, DjangoModelPermissions calls get_queryset() solely to derive
        # the model for the permission check. Short-circuit so the published-case
        # scan / cache lookup does not run on the POST hot path. .none() still
        # carries .model. (update/partial_update fall through to the real lookup.)
        if self.action == "create":
            return JawafEntity.objects.none()

        # For retrieve action, return all entities
        if self.action == "retrieve":
            return JawafEntity.objects.all()

        # The org-wide ReadOnly role gets a system-wide read: every entity in the
        # list, not just those appearing in published cases (mirrors the source
        # widening above). Other callers keep the public, published-only contract.
        user = self.request.user
        if user and user.is_authenticated and is_readonly(user):
            return JawafEntity.objects.all().order_by("-created_at")

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
                ip_address=get_client_ident(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )

            return Response(
                serializer.to_representation(feedback), status=status.HTTP_201_CREATED
            )

        return Response(
            {"error": "Validation error", "details": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )


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
