"""
API ViewSets for the Jawafdehi accountability platform.

See: .kiro/specs/accountability-platform-core/design.md
"""

import ipaddress
import logging
import re
from xml.etree.ElementTree import Element, SubElement, tostring

import jsonpatch
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
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
from rest_framework import filters, status, viewsets
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny, DjangoModelPermissions, IsAuthenticated
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from jawafdehi_shared.drf.auditlog import AuditlogActorMixin, log_bulk_update
from jawafdehi_shared.identity import (
    JAWAFDEHI_USER_ID_HEADER,
    resolve_or_create_identity,
)

from .caseworker_serializers import (
    BLOCKED_PATH_PREFIXES,
    CaseCreateSerializer,
    CasePatchSerializer,
)
from .models import (
    Case,
    CaseEntityRelationship,
    CaseMaterialReference,
    CaseState,
    RelationshipOutcome,
    RelationshipType,
    NewsletterSubscription,
    NewsletterSubscriptionStatus,
    StatisticsSnapshot,
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
    FeedbackSerializer,
    NewsletterSubscriptionSerializer,
    NewsletterUnsubscribeSerializer,
)
from .services.statistics import (
    STATISTICS_SNAPSHOT_KEY,
    bootstrap_placeholder,
    refresh_statistics,
)
from .services.sendpulse import (
    SYNC_STATUS_DISABLED,
    mark_sync_status,
)

logger = logging.getLogger(__name__)


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
        - Admin / Moderator / Caseworker / ReadOnly: all non-CLOSED cases
          (PUBLISHED + IN_REVIEW + DRAFT).
        - Other authenticated users: PUBLISHED cases + any DRAFT or IN_REVIEW cases
          they are explicitly assigned to as contributors.

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

    Read visibility is role-based: unauthenticated/public callers see PUBLISHED
    cases only (DRAFT and IN_REVIEW are casework → 404 for them). Admin /
    Moderator / Caseworker / ReadOnly see all non-CLOSED cases (incl. DRAFT and
    IN_REVIEW). CLOSED cases are never exposed via this API.
    """

    serializer_class = CaseSerializer
    lookup_field = "slug"
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
            "courtcase_references",
        ).order_by("-created_at")

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
            "slug",
            "court_cases",
            "missing_details",
            "bigo",
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
                case.contributors.add(request.user)

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
        # All target states (IN_REVIEW / PUBLISHED / CLOSED / DRAFT) are
        # supported; each dispatches to the corresponding model method below.
        # can_transition_case_state gates the roles: Caseworkers are confined to
        # DRAFT<->IN_REVIEW by the predicate, so PUBLISHED/CLOSED/revert-to-DRAFT
        # are effectively Admin/Moderator only.

        # Gate each join rewrite (entities / evidence / court-case refs) to ops
        # that actually target its path: _build_snapshot always carries these
        # keys, so they are always present in validated after apply_patch, and
        # writing unconditionally would wipe the join on every scalar PATCH.
        # isinstance guard on path: a non-string (malformed client op) must
        # not AttributeError into a 500.
        def _touches(path):
            return any(
                isinstance(op, dict)
                and isinstance(op.get("path"), str)
                and (op["path"] == path or op["path"].startswith(path + "/"))
                for op in patch_ops
            )

        entities_touched = _touches("/entities")
        evidence_touched = _touches("/evidence")
        court_cases_touched = _touches("/court_cases")

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
                # NOTE: "evidence" and "court_cases" are intentionally NOT
                # scalar fields. Neither is a Case column (they are the
                # CaseMaterialReference / CaseCourtCaseReference joins), so they
                # must never be written via Case.objects.update(). They are
                # persisted separately below via _write_material_references /
                # _write_courtcase_references when a /evidence or /court_cases
                # patch op is present.
                "slug",
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
                # ``QuerySet.update()`` bypasses ``post_save``, so auditlog's UPDATE
                # receiver never fires for scalar content edits — the same bypass the
                # explicit search re-index below compensates for. Record the diff
                # explicitly so content changes (description, timeline, allegations, …)
                # are attributable, not just workflow/state saves. ``case`` still holds
                # the pre-update values here (``update()`` doesn't touch the in-memory
                # instance); it is refreshed to the new values on the next line.
                log_bulk_update(
                    case,
                    Case.objects.get(pk=case.pk),
                    fields=list(scalar_updates.keys()),
                )

            # Persist entity relationship changes only when a /entities op
            # was explicitly included — avoids unnecessary delete/recreate on
            # scalar-only PATCHes.
            case.refresh_from_db()
            if entities_touched:
                # Preserve an entity's outcome across the whole-list
                # delete/recreate when the client didn't send one, so an
                # outcome-unaware client/script can't silently reset verdicts
                # to 'charged'. Keyed by (nes_id, relationship_type) — the bind
                # identity — so a re-sent bind keeps its outcome; a new bind (or
                # one whose role changed) falls back to 'charged'.
                prior_outcomes = {
                    (rel.nes_id, rel.relationship_type): rel.outcome
                    for rel in case.entity_relationships.all()
                }
                case.entity_relationships.all().delete()
                for item in validated["entities"]:
                    key = (item["nes_id"], item["relationship_type"])
                    outcome = item.get("outcome") or prior_outcomes.get(
                        key, RelationshipOutcome.CHARGED
                    )
                    CaseEntityRelationship.objects.create(
                        case=case,
                        nes_id=item["nes_id"],
                        relationship_type=item["relationship_type"],
                        outcome=outcome,
                        notes=item.get("notes") or "",
                    )

            # Persist evidence (material-reference) changes only when a /evidence
            # op was explicitly included. Capture the pre-rewrite IRIs so removed
            # materials are recomputed too (a material dropped from a published
            # case must be re-evaluated, else it stays LISTED via a stale referrer).
            affected_material_iris: set[str] = set()
            if evidence_touched:
                affected_material_iris.update(
                    case.material_references.values_list("material_iri", flat=True)
                )
                self._write_material_references(case, validated.get("evidence", []))
                affected_material_iris.update(
                    case.material_references.values_list("material_iri", flat=True)
                )

            # Persist court-case reference changes only when a /court_cases op
            # was explicitly included (same gating rationale as evidence). The
            # model's sync is THE single join writer (no-op when unchanged).
            if court_cases_touched:
                case._sync_courtcase_references(validated.get("court_cases") or [])

            case.refresh_from_db()

            if target_state is not None and target_state != case.state:
                # Every target dispatches to the model method that already
                # implements + validates the transition (Case.validate() enforces
                # BR-1..BR-4 on IN_REVIEW/PUBLISHED). No transition rule is
                # re-implemented here; the permission gate was applied above via
                # can_transition_case_state. A model ValidationError -> 422 with
                # field-keyed messages (mirroring the original submit() handling).
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
                    detail = getattr(exc, "message_dict", None) or {
                        "detail": exc.messages
                    }
                    transaction.set_rollback(True)
                    return Response(detail, status=status.HTTP_422_UNPROCESSABLE_ENTITY)

        # Scalar edits go through queryset .update() and entity-relationship
        # edits through bulk delete/create — neither fires post_save, so the
        # live search-index signal never runs. Re-index explicitly (best-effort,
        # on_commit) so a PUBLISHED case's content/slug/relationships stay fresh
        # in the index; a non-PUBLISHED case is evicted by the same call.
        from .search_index import index as _index_case

        transaction.on_commit(lambda: _index_case(case))

        # A state transition OR an evidence-set change alters the visibility of
        # the referenced materials (visibility = MAX over referring case states).
        # Recompute the union of currently-referenced + just-removed materials so
        # a demoted case can't leave stale-LISTED evidence behind (ADR draft-leak
        # guard). Skipped only on pure scalar/entity PATCHes.
        if evidence_touched or (target_state is not None):
            affected_material_iris.update(
                case.material_references.values_list("material_iri", flat=True)
            )
            _recompute_material_visibility(affected_material_iris)

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
        }


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
        snapshot = StatisticsSnapshot.objects.filter(
            pk=STATISTICS_SNAPSHOT_KEY
        ).first()
        if snapshot is not None:
            # A bootstrap-placeholder row (committed by the claim below while
            # the winning request is still computing) must never be pinned at
            # the edge — its zeroed blocks would be served worldwide for a
            # full TTL. Real snapshots are publicly cacheable.
            cache_control = (
                "no-store" if snapshot.is_placeholder else self.CACHE_CONTROL
            )
            return Response(
                snapshot.data, headers={"Cache-Control": cache_control}
            )
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


def _client_ip(request):
    """Extract the client IP, preferring X-Forwarded-For behind a reverse proxy.

    The app runs behind a proxy (``SECURE_PROXY_SSL_HEADER`` is set), so
    ``REMOTE_ADDR`` is the proxy's address — using it alone would put every
    client in one rate-limit bucket and store the proxy IP as the consent IP.
    Mirrors ``FeedbackView.get_client_ip``.

    The forwarded value is client-supplied, so a malformed token is validated and
    rejected before it can be stored in ``NewsletterSubscription.ip_address`` (a
    ``GenericIPAddressField``), where an invalid value would raise ``DataError``
    on insert and turn the public signup into a 500.
    """
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        candidate = x_forwarded_for.split(",")[0].strip()
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            pass  # malformed header — fall back to REMOTE_ADDR
        else:
            return candidate
    return request.META.get("REMOTE_ADDR")


def _enqueue_sendpulse_sync(subscription):
    """Offload the subscription's SendPulse sync onto the central job queue.

    The provider HTTP call can block for the configured timeout, so it must not
    run on the public request thread. A ``newsletter_sendpulse`` job carries the
    subscription id; the queue consumer resolves the current state and pushes it
    (see ``jobs.consumers`` / ``cases.job_handlers``). When SendPulse is disabled
    there is nothing to offload, so the disabled state is recorded inline rather
    than queuing a no-op job that would sit unclaimed.
    """
    if not settings.SENDPULSE_ENABLED:
        mark_sync_status(subscription.pk, SYNC_STATUS_DISABLED)
        return

    from jobs import queue as job_queue

    transaction.on_commit(
        lambda pk=subscription.pk: job_queue.enqueue(
            "newsletter_sendpulse",
            payload={"subscription_id": pk},
            dedup_key=f"newsletter_sendpulse:{pk}",
        )
    )


class NewsletterSubscriptionRateThrottle(AnonRateThrottle):
    """Rate throttle for newsletter signups: 10 per hour."""

    rate = "10/hour"

    def get_ident(self, request):
        return _client_ip(request) or ""


class NewsletterUnsubscribeRateThrottle(NewsletterSubscriptionRateThrottle):
    """Rate throttle for unsubscribe token submissions: 30 per hour."""

    rate = "30/hour"


@extend_schema(
    summary="Subscribe to the Jawafdehi newsletter",
    description="""
    Subscribe an email address to Jawafdehi newsletter updates.

    Rate limited to 10 submissions per IP address per hour. Re-submitting an
    already-unsubscribed email does not restore subscribed status; unsubscribe
    reversal requires a future email-confirmed flow.
    """,
    request=NewsletterSubscriptionSerializer,
    responses={
        201: NewsletterSubscriptionSerializer,
        400: OpenApiTypes.OBJECT,
        409: OpenApiTypes.OBJECT,
        429: OpenApiTypes.OBJECT,
    },
    examples=[
        OpenApiExample(
            "Newsletter Signup",
            value={
                "firstName": "Ram",
                "lastName": "Bahadur",
                "email": "ram@example.com",
                "consentAccepted": True,
                "consentSource": "share_our_vision",
                "privacyVersion": "2026-07-06",
            },
            request_only=True,
        ),
    ],
)
class NewsletterSubscriptionView(APIView):
    """API view for public newsletter subscriptions."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [NewsletterSubscriptionRateThrottle]
    parser_classes = [JSONParser, FormParser]

    def post(self, request):
        serializer = NewsletterSubscriptionSerializer(data=request.data)

        if serializer.is_valid():
            subscription = serializer.save(
                ip_address=self.get_client_ip(request),
                user_agent=request.META.get("HTTP_USER_AGENT", ""),
            )
            if subscription.status == NewsletterSubscriptionStatus.UNSUBSCRIBED:
                return Response(
                    {
                        "error": "This address was previously unsubscribed.",
                        "code": "newsletter_unsubscribed",
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            _enqueue_sendpulse_sync(subscription)
            return Response(
                serializer.to_representation(subscription),
                status=status.HTTP_201_CREATED,
            )

        return Response(
            {"error": "Validation error", "details": serializer.errors},
            status=status.HTTP_400_BAD_REQUEST,
        )

    def get_client_ip(self, request):
        return _client_ip(request)


@extend_schema(
    summary="Unsubscribe from the Jawafdehi newsletter",
    description="Unsubscribe a recipient using the opaque token from a newsletter email.",
    responses={
        200: NewsletterUnsubscribeSerializer,
        404: OpenApiTypes.OBJECT,
        429: OpenApiTypes.OBJECT,
    },
)
class NewsletterUnsubscribeView(APIView):
    """API view for token-based newsletter unsubscribes."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [NewsletterUnsubscribeRateThrottle]
    parser_classes = [JSONParser, FormParser]

    def post(self, request, token):
        try:
            subscription = NewsletterSubscription.objects.get(unsubscribe_token=token)
        except NewsletterSubscription.DoesNotExist:
            return Response(
                {"error": "Invalid unsubscribe token"},
                status=status.HTTP_404_NOT_FOUND,
            )

        if subscription.status != NewsletterSubscriptionStatus.UNSUBSCRIBED:
            subscription.status = NewsletterSubscriptionStatus.UNSUBSCRIBED
            subscription.unsubscribed_at = timezone.now()
            subscription.save(update_fields=["status", "unsubscribed_at", "updated_at"])
        _enqueue_sendpulse_sync(subscription)
        serializer = NewsletterUnsubscribeSerializer(subscription)
        return Response(serializer.data, status=status.HTTP_200_OK)


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
