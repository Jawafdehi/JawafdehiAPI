"""NGM read-plane viewsets (public reads) + the internal write/query planes.

Ports the FastAPI NGM API plane to DRF:

- Read plane (public, ``AllowAny``): courts, cases + the case sub-resources
  hearings / entities / documents, an entity-resolution search, and blacklisted
  firms. List endpoints use the shared ``PlatformCursorPagination`` so the wire
  shape is the platform ``{results, next}`` (the existing converted read plane's
  contract), not the FastAPI ``{items, next_cursor}``.
- Gated SQL plane (``POST /query``): OIDC + NGM-role gated; the query_guard
  policy (SELECT-only, allowlist, scraped_dates blocked, row cap, statement
  timeout) is enforced in the view.
- Ingestion plane (``POST /ingestion/*``): OIDC + NGM-role gated write stubs
  (501), mirroring the FastAPI ingestion routes.

Search is NOT served here: the former NGM ``GET /search`` 501 stub was removed in
the unified-search cutover. Platform search lives at ``GET /api/search/`` (the
``search`` app), which indexes NGM materials + court cases alongside entities and
cases.
"""

from __future__ import annotations

import time

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connections, router
from rest_framework import mixins, status, viewsets

from jawafdehi_shared.entities.ids import is_valid_entity_iri
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import query_guard
from .models import BlacklistedFirm, CaseEntity, Court, CourtCase, CourtCaseHearing
from .normalize import best_effort_normalize
from .permissions import HasNgmQueryAccess, HasNgmRole
from .serializers import (
    BlacklistedFirmSerializer,
    CaseEntitySerializer,
    CourtCaseHearingSerializer,
    CourtCaseSerializer,
    CourtCaseWriteSerializer,
    CourtSerializer,
)


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    return Response({"status": "ok", "service": "ngm-api"})


# Write-method names shared by the per-method permission gate below. Reads
# (GET/HEAD/OPTIONS + the custom retrieve_composite/list_* actions) stay public;
# create/update require an NGM role.
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class _PublicReadNgmWriteMixin:
    """Per-method permissions: public reads, NGM-role-gated writes.

    Mirrors ``entities.views.EntityListCreateView.get_permissions``:
    write methods require ``HasNgmRole``, everything else is ``AllowAny``.
    """

    def get_permissions(self):
        if self.request.method in _WRITE_METHODS:
            return [HasNgmRole()]
        return [AllowAny()]


class CourtViewSet(
    _PublicReadNgmWriteMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    queryset = Court.objects.all().order_by("identifier")
    serializer_class = CourtSerializer
    pagination_class = None           # small fixed set (~97 rows)


class CourtCaseViewSet(
    _PublicReadNgmWriteMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    viewsets.GenericViewSet,
):
    """Cases + the hearings / entities / documents sub-resources.

    The case detail/sub-resources are addressed as ``/cases/{court}/{case_number}``
    (composite key), matching the FastAPI routes. The list endpoint supports the
    same filters as FastAPI (court / type / status / date_from / date_to).

    Writes: ``POST /cases/`` creates a case (CreateModelMixin, gated by the
    per-method permission mixin); ``PUT|PATCH /cases/{court}/{case_number}``
    updates via ``update_composite`` (wired in urls.py — the composite PK has no
    single ``pk`` the router can address).
    """

    serializer_class = CourtCaseSerializer

    def get_serializer_class(self):
        # Writes use the dedicated write serializer (FK/PK handling); reads use
        # the read serializer (derived material_id / courtcase_iri fields).
        if self.request and self.request.method in _WRITE_METHODS:
            return CourtCaseWriteSerializer
        return CourtCaseSerializer

    def get_queryset(self):
        qs = CourtCase.objects.all().order_by("-created_at")
        params = self.request.query_params
        if (court := params.get("court")):
            qs = qs.filter(court_id=court)
        if (case_type := params.get("type")):
            qs = qs.filter(case_type=case_type)
        if (case_status := params.get("status")):
            qs = qs.filter(case_status=case_status)
        if (date_from := params.get("date_from")):
            qs = qs.filter(registration_date_ad__gte=date_from)
        if (date_to := params.get("date_to")):
            qs = qs.filter(registration_date_ad__lte=date_to)
        return qs

    def _get_case_or_404(self, court: str, case_number: str) -> CourtCase:
        from django.shortcuts import get_object_or_404

        return get_object_or_404(
            CourtCase, court_id=court, case_number=best_effort_normalize(case_number)
        )

    # --- detail + sub-resources, keyed on (court, case_number) ----------------
    # Routed explicitly in urls.py rather than via the router's pk lookup, so the
    # composite key in the path works ( /cases/{court}/{case_number}[/...] ).

    def create(self, request, *args, **kwargs):
        """``POST /cases/`` — create a case from the composite natural key.

        Uses the write serializer (court_identifier -> court FK). ``nes_id`` is
        IRI-validated by the serializer field validator; ``full_clean()`` re-runs
        the model field validators before save so a bad nes_id is a 400, never a
        500. The 201 body is the READ serializer (derived material_id etc.).
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        instance = CourtCase(**serializer.validated_data)
        try:
            instance.full_clean(validate_unique=False)
        except DjangoValidationError as exc:
            raise DRFValidationError(exc.message_dict)
        instance.save()
        return Response(
            CourtCaseSerializer(instance).data, status=status.HTTP_201_CREATED
        )

    def update_composite(self, request, court: str, case_number: str):
        """``PUT|PATCH /cases/{court}/{case_number}`` — update a case.

        Loads via the composite natural key (``_get_case_or_404``) and applies the
        write serializer. PATCH is partial; PUT is a full update. The response is
        the READ serializer (material_id / courtcase_iri round-trip).
        """
        case = self._get_case_or_404(court, case_number)
        partial = request.method == "PATCH"
        serializer = CourtCaseWriteSerializer(case, data=request.data, partial=partial)
        serializer.is_valid(raise_exception=True)
        for attr, value in serializer.validated_data.items():
            setattr(case, attr, value)
        try:
            case.full_clean(validate_unique=False)
        except DjangoValidationError as exc:
            raise DRFValidationError(exc.message_dict)
        case.save()
        return Response(CourtCaseSerializer(case).data)

    def retrieve_composite(self, request, court: str, case_number: str):
        case = self._get_case_or_404(court, case_number)
        return Response(CourtCaseSerializer(case).data)

    def list_hearings(self, request, court: str, case_number: str):
        case = self._get_case_or_404(court, case_number)
        qs = CourtCaseHearing.objects.filter(
            court_id=court, case_number=case.case_number
        ).order_by("-id")
        return self._paginated(qs, CourtCaseHearingSerializer)

    def list_entities(self, request, court: str, case_number: str):
        case = self._get_case_or_404(court, case_number)
        qs = CaseEntity.objects.filter(
            court_id=court, case_number=case.case_number
        ).order_by("id")
        return self._paginated(qs, CaseEntitySerializer)

    def list_documents(self, request, court: str, case_number: str):
        """DocumentSource modality (roled links) — inline JSONB list on the case."""
        case = self._get_case_or_404(court, case_number)
        docs = [d for d in (case.document_sources or []) if isinstance(d, dict)]
        return Response({"results": docs, "next": None})

    def _paginated(self, queryset, serializer_class):
        page = self.paginate_queryset(queryset)
        if page is not None:
            return self.get_paginated_response(serializer_class(page, many=True).data)
        return Response(serializer_class(queryset, many=True).data)


class CaseEntityViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Party-resolution surface across cases (``/entities``), filter by nes_id or name."""

    serializer_class = CaseEntitySerializer
    permission_classes = [AllowAny]

    def get_queryset(self):
        qs = CaseEntity.objects.all().order_by("id")
        params = self.request.query_params
        if (nes_id := params.get("nes_id")):
            qs = qs.filter(nes_id=nes_id)
        if (name := params.get("name")):
            qs = qs.filter(name__icontains=name)
        return qs


class BlacklistedFirmViewSet(
    _PublicReadNgmWriteMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.CreateModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """PPMO-blacklisted firms (``/firms``). Public reads; NGM-gated writes."""

    serializer_class = BlacklistedFirmSerializer

    def get_queryset(self):
        qs = BlacklistedFirm.objects.all().order_by("-blacklist_date_ad", "id")
        params = self.request.query_params
        if (nes_id := params.get("nes_id")):
            qs = qs.filter(nes_id=nes_id)
        if (name := params.get("name")):
            qs = qs.filter(firm_name__icontains=name)
        return qs


# --- gated SQL plane --------------------------------------------------------


class QueryView(APIView):
    """``POST /query`` — gated raw-SQL for power users / MCP.

    OIDC-gated on EITHER the ``ngm.query`` OAuth scope OR an NGM role (see
    ``HasNgmQueryAccess`` — restores the FastAPI route's scope control while
    keeping role-based access). The body is
    ``{"query": "<SELECT ...>", "timeout_seconds"?}``. Enforces the query_guard
    policy then runs the validated, row-capped SELECT with a per-statement
    timeout. DB errors surface as a generic 400 (no internals leaked), matching
    the FastAPI route.
    """

    permission_classes = [HasNgmQueryAccess]

    def post(self, request):
        body = request.data if isinstance(request.data, dict) else {}
        sql = body.get("query")
        if not isinstance(sql, str) or not sql.strip():
            return Response(
                {"detail": "A single SELECT statement is required in 'query'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ok, error = query_guard.validate_query(sql)
        if not ok:
            return Response({"detail": error}, status=status.HTTP_400_BAD_REQUEST)

        max_rows = query_guard.default_max_rows()
        timeout = body.get("timeout_seconds") or query_guard.default_timeout_seconds()
        try:
            timeout = float(timeout)
            if timeout <= 0 or timeout > 120:
                raise ValueError
        except (TypeError, ValueError):
            return Response(
                {"detail": "timeout_seconds must be a number in (0, 120]."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        capped = query_guard.apply_row_cap(sql, max_rows)
        try:
            result = self._execute_select(capped, timeout_seconds=timeout)
        except Exception:  # noqa: BLE001 — never leak DB internals to the caller
            return Response(
                {"detail": "Database query failed"}, status=status.HTTP_400_BAD_REQUEST
            )
        result["max_rows"] = max_rows
        return Response(result)

    @staticmethod
    def _execute_select(query: str, *, timeout_seconds: float) -> dict:
        """Run a validated SELECT with a statement timeout; return columns/rows.

        Mirrors the FastAPI ``PostgresRawQueryExecutor``. On Postgres a
        per-statement ``SET LOCAL statement_timeout`` bounds runtime; on other
        backends (e.g. sqlite in tests) the SET is skipped.

        Runs against the NGM database connection (the one the DB router pins the
        courts models to), NOT the ``default`` connection — after the platform
        collapse the courts/materials tables live in the ``ngm`` alias, so the
        raw-SQL ``FROM courts`` etc. must target that connection.
        """
        ngm_alias = router.db_for_read(CourtCase)
        connection = connections[ngm_alias]
        timeout_ms = int(timeout_seconds * 1000)
        start = time.perf_counter()
        with connection.cursor() as cursor:
            if connection.vendor == "postgresql":
                cursor.execute("SET statement_timeout = %s", [timeout_ms])
            cursor.execute(query)
            columns = [c[0] for c in cursor.description] if cursor.description else []
            rows = [list(r) for r in cursor.fetchall()]
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "query_time_ms": int((time.perf_counter() - start) * 1000),
        }


# --- ingestion plane (internal, write stubs) --------------------------------


class _IngestionStub(APIView):
    """Shared base for the OIDC + NGM-role gated ingestion write stubs."""

    permission_classes = [HasNgmRole]
    _detail = "Ingestion plane not yet implemented"

    def post(self, request, *args, **kwargs):
        # TODO(ingestion): idempotent bulk upsert by natural key / nes_id
        # write-back / document-source registration via a write-capable layer.
        return Response(
            {"detail": self._detail}, status=status.HTTP_501_NOT_IMPLEMENTED
        )


class IngestionCasesView(_IngestionStub):
    """``POST /ingestion/cases`` — bulk upsert scraped cases (natural key)."""


class IngestionEntitiesResolveView(_IngestionStub):
    """``POST /ingestion/entities/resolve`` — write-back nes_id from NES.

    Clean-slate contract: every ``nes_id`` written is the canonical entity ``@id``
    IRI (``https://<base>/entity/<prefix>/<slug>``). The resolution payload is
    IRI-validated at this boundary BEFORE the (still-stubbed) write-back, so a
    non-IRI ``nes_id`` is a 400 and never reaches the tables — the same contract
    the ``CaseEntity.nes_id`` field validator enforces.
    """

    _detail = "Entity resolution write-back not yet implemented"

    def post(self, request, *args, **kwargs):
        items = request.data.get("items") if isinstance(request.data, dict) else None
        if isinstance(items, list):
            for item in items:
                nes_id = item.get("nes_id") if isinstance(item, dict) else None
                if nes_id is not None and not is_valid_entity_iri(nes_id):
                    return Response(
                        {
                            "detail": (
                                f"nes_id must be a canonical entity @id IRI "
                                f"(https://<base>/entity/<prefix>/<slug>); got {nes_id!r}."
                            )
                        },
                        status=status.HTTP_400_BAD_REQUEST,
                    )
        return super().post(request, *args, **kwargs)


class IngestionDocumentsView(_IngestionStub):
    """``POST /ingestion/documents`` — register document_sources (roled links)."""


# --- search plane -----------------------------------------------------------
# The former NGM ``GET /search`` 501 stub was REMOVED in the unified-search
# cutover (plan decision #5). Platform-wide search now lives in the ``search``
# app at ``GET /api/search/`` (``search``), which indexes NGM materials
# and court cases into OpenSearch alongside NES entities and Jawafdehi cases.
