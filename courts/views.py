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
from django.db import connections, router, transaction
from rest_framework import mixins, status, viewsets

from jawafdehi_shared.drf.auditlog import AuditlogActorMixin
from jawafdehi_shared.entities.ids import is_valid_entity_iri
from rest_framework.decorators import api_view, permission_classes
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import query_guard, search_index
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


class _PublicReadNgmWriteMixin(AuditlogActorMixin):
    """Per-method permissions: public reads, NGM-role-gated writes.

    Mirrors ``entities.views.EntityListCreateView.get_permissions``:
    write methods require ``HasNgmRole``, everything else is ``AllowAny``.

    Also mixes in ``AuditlogActorMixin`` so any audit entry produced by a courts
    write is attributed to the authenticated user. (Courts models aren't
    auditlog-registered today, so this is inert until they are — it's here so the
    actor-capture seam is uniform across the platform's write viewsets.)
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
        # Soft-deleted cases are excluded from the read plane.
        qs = CourtCase.objects.filter(is_deleted=False).order_by("-created_at")
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

        # Soft-deleted cases are not addressable on the read plane (404).
        return get_object_or_404(
            CourtCase,
            court_id=court,
            case_number=best_effort_normalize(case_number),
            is_deleted=False,
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

    def destroy_composite(self, request, court: str, case_number: str):
        """``DELETE /courtcases/{court}/{case_number}`` — soft-delete a case.

        Flips ``is_deleted=True`` so the case vanishes from the read plane but is
        never hard-removed (accountability/audit platform). NGM-role gated (via
        the per-method permission mixin); returns 204.
        """
        case = self._get_case_or_404(court, case_number)
        case.is_deleted = True
        case.save(update_fields=["is_deleted", "updated_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)

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


# --- ingestion plane (internal, real bulk writers) --------------------------
# The FastAPI ingestion routes, ported to real DRF writers. All are OIDC +
# NGM-role gated (``HasNgmRole``) and operate on a ``{"items": [...]}`` body:
# each is an idempotent bulk write keyed on a natural key, returning per-item and
# aggregate counts so a scraper can safely re-run a batch.


class _IngestionView(AuditlogActorMixin, APIView):
    """Shared base for the OIDC + NGM-role gated ingestion writers.

    ``AuditlogActorMixin`` attributes any audit entry to the authenticated user
    (inert until courts models are auditlog-registered; see
    ``_PublicReadNgmWriteMixin``)."""

    permission_classes = [HasNgmRole]

    @staticmethod
    def _items(request) -> list | None:
        """Return the ``items`` list from the body, or ``None`` if malformed."""
        body = request.data if isinstance(request.data, dict) else None
        if body is None:
            return None
        items = body.get("items")
        return items if isinstance(items, list) else None

    @staticmethod
    def _bad_items() -> Response:
        return Response(
            {"detail": "Body must be an object with an 'items' list."},
            status=status.HTTP_400_BAD_REQUEST,
        )


class IngestionCasesView(_IngestionView):
    """``POST /ingestion/cases`` — idempotent bulk upsert of court cases.

    Body: ``{"items": [{"court"|"court_identifier", "case_number", ...fields}]}``.
    Each item is upserted by the natural key (court_identifier, case_number)
    reusing ``CourtCaseWriteSerializer`` + the same create/update logic as the
    ``courtcases`` viewset (``full_clean`` before save, so a bad ``nes_id`` is a
    400). Re-running the same batch is a no-op (rows already exist → updated).
    A soft-deleted case is REVIVED by an upsert (the write is the source of
    truth). Returns per-item results + ``{created, updated, failed}`` counts.
    """

    def post(self, request, *args, **kwargs):
        items = self._items(request)
        if items is None:
            return self._bad_items()

        results: list[dict] = []
        created = updated = failed = 0
        for index, raw in enumerate(items):
            outcome, err = self._upsert_case(raw)
            if err is not None:
                failed += 1
                results.append({"index": index, "status": "failed", "errors": err})
                continue
            if outcome == "created":
                created += 1
            else:
                updated += 1
            results.append({"index": index, "status": outcome})

        return Response(
            {"created": created, "updated": updated, "failed": failed, "results": results}
        )

    @staticmethod
    def _upsert_case(raw):
        """Upsert one case item. Returns ("created"|"updated", None) or (None, errors)."""
        if not isinstance(raw, dict):
            return None, {"detail": "Each item must be an object."}
        # Accept either the write serializer's ``court_identifier`` wire field or
        # a plain ``court`` alias (the FastAPI ingestion / scraper shape).
        data = dict(raw)
        if "court_identifier" not in data and "court" in data:
            data["court_identifier"] = data.pop("court")
        case_number = data.get("case_number")
        court_identifier = data.get("court_identifier")
        if not case_number or not court_identifier:
            return None, {"detail": "case_number and court (identifier) are required."}

        normalized_number = best_effort_normalize(str(case_number))
        data["case_number"] = normalized_number
        existing = CourtCase.objects.filter(
            court_id=court_identifier, case_number=normalized_number
        ).first()

        serializer = CourtCaseWriteSerializer(
            existing, data=data, partial=existing is not None
        )
        if not serializer.is_valid():
            return None, serializer.errors

        is_create = existing is None
        instance = existing or CourtCase()
        for attr, value in serializer.validated_data.items():
            setattr(instance, attr, value)
        # An upsert is the source of truth: revive a soft-deleted row.
        instance.is_deleted = False
        try:
            instance.full_clean(validate_unique=False)
        except DjangoValidationError as exc:
            return None, exc.message_dict
        instance.save()
        return ("created" if is_create else "updated"), None


class IngestionEntitiesResolveView(_IngestionView):
    """``POST /ingestion/entities/resolve`` — write-back nes_id onto case parties.

    Clean-slate contract: every ``nes_id`` is the canonical entity ``@id`` IRI
    (``https://<base>/entity/<prefix>/<slug>``) — IRI-validated at this boundary
    BEFORE any write, so a non-IRI ``nes_id`` is a 400 and never reaches the
    tables (same contract as the ``CaseEntity.nes_id`` field validator).

    SAFE SUBSET (documented): this attaches/verifies a resolved ``nes_id`` on the
    matching ``CaseEntity`` rows. An item targets rows by ``(court, case_number)``
    plus an optional ``side`` and/or ``name`` filter; every matching party row
    gets its ``nes_id`` set. It does NOT create parties, fuzzy-match names, or
    resolve against NES — only writes back an already-resolved IRI onto existing
    rows (returning the count matched/updated per item, plus ``unmatched`` when a
    filter hit no rows). Broader auto-resolution is intentionally out of scope.
    """

    def post(self, request, *args, **kwargs):
        items = self._items(request)
        if items is None:
            return self._bad_items()

        # Validate every nes_id IRI up front (reject the whole batch on a bad one,
        # so a partial write never leaves a mix of resolved/unresolved rows).
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

        results: list[dict] = []
        resolved = unmatched = failed = 0
        for index, raw in enumerate(items):
            count, err = self._resolve_item(raw)
            if err is not None:
                failed += 1
                results.append({"index": index, "status": "failed", "errors": err})
            elif count == 0:
                unmatched += 1
                results.append({"index": index, "status": "unmatched", "matched": 0})
            else:
                resolved += count
                results.append({"index": index, "status": "resolved", "matched": count})

        return Response(
            {
                "resolved": resolved,
                "unmatched": unmatched,
                "failed": failed,
                "results": results,
            }
        )

    @staticmethod
    def _resolve_item(raw):
        """Attach nes_id to matching CaseEntity rows. Returns (matched_count, None)
        or (None, errors)."""
        if not isinstance(raw, dict):
            return None, {"detail": "Each item must be an object."}
        court_identifier = raw.get("court") or raw.get("court_identifier")
        case_number = raw.get("case_number")
        nes_id = raw.get("nes_id")
        if not court_identifier or not case_number:
            return None, {"detail": "court (identifier) and case_number are required."}
        if not nes_id:
            return None, {"detail": "nes_id is required to resolve a party."}

        qs = CaseEntity.objects.filter(
            court_id=court_identifier,
            case_number=best_effort_normalize(str(case_number)),
        )
        if (side := raw.get("side")):
            qs = qs.filter(side=side)
        if (name := raw.get("name")):
            qs = qs.filter(name=name)
        matched = qs.update(nes_id=nes_id)
        # A bulk .update() bypasses the CaseEntity post_save reindex signal, so
        # the parent CourtCase search doc (which folds party nes_id IRIs into its
        # ``identifiers``) would go stale. Re-index the parent on commit, mirroring
        # signals._reindex_parent_courtcase — once per resolved case, not per row.
        if matched:
            case = CourtCase.objects.filter(
                court_id=court_identifier,
                case_number=best_effort_normalize(str(case_number)),
                is_deleted=False,
            ).first()
            if case is not None:
                transaction.on_commit(lambda c=case: search_index.index(c))
        return matched, None


class IngestionDocumentsView(_IngestionView):
    """``POST /ingestion/documents`` — register document sources onto court cases.

    Body: ``{"items": [{"court"|"court_identifier", "case_number",
    "document_source": {"document_id", "url": [{"link", "role"}], ...}}]}``.
    Each item appends its ``document_source`` (a roled-link DocumentSource, the
    shape already stored in the ``document_sources`` JSONB list on ``CourtCase``
    and projected to schema.org ``associatedMedia`` by
    ``materials.jsonld.media_objects_from_document_sources``) onto the target
    case. Idempotent: an entry whose ``document_id`` already exists on the case
    is REPLACED in place (not duplicated); entries without a ``document_id`` are
    appended. Returns per-item results + ``{updated, unmatched, failed}``.

    NOTE (documented scope): this registers document sources onto EXISTING court
    cases (the court-order/document model in this codebase is the case's inline
    ``document_sources`` list — there is no standalone document row to create).
    A missing case is reported ``unmatched`` (not created here).
    """

    def post(self, request, *args, **kwargs):
        items = self._items(request)
        if items is None:
            return self._bad_items()

        results: list[dict] = []
        updated = unmatched = failed = 0
        for index, raw in enumerate(items):
            outcome, err = self._register_document(raw)
            if err is not None:
                failed += 1
                results.append({"index": index, "status": "failed", "errors": err})
            elif outcome == "unmatched":
                unmatched += 1
                results.append({"index": index, "status": "unmatched"})
            else:
                updated += 1
                results.append({"index": index, "status": "updated"})

        return Response(
            {
                "updated": updated,
                "unmatched": unmatched,
                "failed": failed,
                "results": results,
            }
        )

    @staticmethod
    def _register_document(raw):
        """Append/replace a document_source on the target case. Returns
        ("updated"|"unmatched", None) or (None, errors)."""
        if not isinstance(raw, dict):
            return None, {"detail": "Each item must be an object."}
        court_identifier = raw.get("court") or raw.get("court_identifier")
        case_number = raw.get("case_number")
        document_source = raw.get("document_source")
        if not court_identifier or not case_number:
            return None, {"detail": "court (identifier) and case_number are required."}
        if not isinstance(document_source, dict):
            return None, {"detail": "document_source must be an object."}

        case = CourtCase.objects.filter(
            court_id=court_identifier,
            case_number=best_effort_normalize(str(case_number)),
            is_deleted=False,
        ).first()
        if case is None:
            return "unmatched", None

        sources = [d for d in (case.document_sources or []) if isinstance(d, dict)]
        document_id = document_source.get("document_id")
        replaced = False
        if document_id:
            for i, existing in enumerate(sources):
                if existing.get("document_id") == document_id:
                    sources[i] = document_source
                    replaced = True
                    break
        if not replaced:
            sources.append(document_source)
        case.document_sources = sources
        try:
            case.full_clean(validate_unique=False)
        except DjangoValidationError as exc:
            return None, exc.message_dict
        case.save(update_fields=["document_sources", "updated_at"])
        return "updated", None


# --- search plane -----------------------------------------------------------
# The former NGM ``GET /search`` 501 stub was REMOVED in the unified-search
# cutover (plan decision #5). Platform-wide search now lives in the ``search``
# app at ``GET /api/search/`` (``search``), which indexes NGM materials
# and court cases into OpenSearch alongside NES entities and Jawafdehi cases.
