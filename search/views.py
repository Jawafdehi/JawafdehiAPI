"""The unified platform search API: ``GET /api/search/`` and the click beacon
``POST /api/search/click``.

Public read. One query across entities, materials, court cases, and PUBLISHED
cases (the index is all-public — no ACL filter). OpenSearch is a hard dependency:
if the cluster is unreachable the endpoint returns 503 (no in-process fallback).

This REPLACES the old Jawafdehi-scoped ``cases.UnifiedSearchView`` and the NGM
501 search stub.
"""

from __future__ import annotations

import json
import time
import uuid

import sentry_sdk
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .analytics import emit_search_click_event, emit_search_event
from .service import (
    ALL_SORTS,
    ALL_TYPES,
    MAX_PAGE_SIZE,
    MAX_TAGS_FACET_SIZE,
    SORT_RELEVANCE,
    TAGS_FACET_SIZE,
    SearchError,
    SearchService,
    SearchUnavailable,
)


class SearchQuerySerializer(serializers.Serializer):
    # ``q`` is OPTIONAL: an empty/absent query becomes a browse (match-all) so the
    # endpoint can list/page the corpus and apply facet filters/sort without a
    # search term (e.g. "all entities of type X, newest first").
    q = serializers.CharField(required=False, allow_blank=True, default="")
    # ``all`` is accepted as an explicit alias for "no type filter" (search every
    # type) — the SPA sends ``?type=all`` for its default/reset state. It is
    # normalized away in ``validate_type`` so the service sees an empty list.
    type = serializers.ListField(
        child=serializers.ChoiceField(choices=[*ALL_TYPES, "all"]),
        required=False,
        default=list,
    )

    def validate_type(self, value):
        # Drop the ``all`` sentinel — an empty list means "search all types".
        return [t for t in value if t != "all"]

    def validate_case_type(self, value):
        # The indexed ``case_type`` token is upper-cased (Jawafdehi enums already
        # are; court-case scraper values are normalized at index time). Upper-case
        # the exact-match filter too so ``?case_type=corruption`` matches the
        # indexed ``CORRUPTION`` — otherwise the terms query silently returns nothing.
        return [t.upper() for t in value]
    lang = serializers.ChoiceField(
        choices=["ne", "en", "both"], required=False, default="both"
    )
    sort = serializers.ChoiceField(
        choices=list(ALL_SORTS), required=False, default=SORT_RELEVANCE
    )
    # Exact-match refine facets. Each narrows the result set and composes with the
    # text query. ``entity_type`` filters the schema.org ``type`` token; ``tags``
    # filters the shared ``keywords`` field.
    entity_type = serializers.ListField(
        child=serializers.CharField(allow_blank=False), required=False, default=list
    )
    case_type = serializers.ListField(
        child=serializers.CharField(allow_blank=False), required=False, default=list
    )
    tags = serializers.ListField(
        child=serializers.CharField(allow_blank=False), required=False, default=list
    )
    # Coarse case lifecycle refine facet (ongoing/closed/others). Case-scoped in
    # practice (the case list sends ?type=case&status=...); the ``status`` param
    # filters the ``case_status`` keyword field (NOT the generic ``status`` field,
    # which holds NGM's scraper enrichment flag).
    status = serializers.ListField(
        child=serializers.CharField(allow_blank=False), required=False, default=list
    )
    # Court-case refine facets, split out of the scraped ``case_subject``:
    # ``charge`` is the normalized head, ``statute_section`` the ``(दफा …)``
    # citation with its digits folded to one script. Court-scoped in practice.
    charge = serializers.ListField(
        child=serializers.CharField(allow_blank=False), required=False, default=list
    )
    statute_section = serializers.ListField(
        child=serializers.CharField(allow_blank=False), required=False, default=list
    )
    page = serializers.IntegerField(required=False, min_value=1, default=1)
    page_size = serializers.IntegerField(
        required=False, min_value=1, max_value=MAX_PAGE_SIZE, default=10
    )
    # How many tag buckets the tags facet returns. The default is deliberately
    # short (design.md §12: cap the initial list), and THIS is the other half of
    # that rule — the client asking for the tail rather than it being unreachable.
    # Bounds/default come from the service constants so the two cannot drift.
    tags_limit = serializers.IntegerField(
        required=False,
        min_value=1,
        max_value=MAX_TAGS_FACET_SIZE,
        default=TAGS_FACET_SIZE,
    )
    # Opaque deep-paging cursor (the ``next_cursor`` from a prior response). When
    # given, ``page`` is ignored and results resume after that point (search_after).
    cursor = serializers.CharField(required=False, allow_blank=False)


@extend_schema(
    summary="Unified platform search",
    description=(
        "One bilingual (Nepali + English) query across NES entities, NGM "
        "materials, NGM court cases, and PUBLISHED Jawafdehi cases. Results are "
        "ranked across types by relevance and returned in one common envelope "
        "with per-type facet counts. Public read; the index contains only public "
        "documents. Backed by OpenSearch — returns 503 if the cluster is down."
    ),
    parameters=[
        OpenApiParameter("q", OpenApiTypes.STR, OpenApiParameter.QUERY, required=True),
        OpenApiParameter(
            "type",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            enum=list(ALL_TYPES),
            many=True,
        ),
        OpenApiParameter(
            "lang",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            enum=["ne", "en", "both"],
        ),
        OpenApiParameter(
            "sort",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            enum=list(ALL_SORTS),
            description="Result ordering. Defaults to relevance.",
        ),
        OpenApiParameter(
            "entity_type",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            many=True,
            description="Refine facet: filter by schema.org type token.",
        ),
        OpenApiParameter(
            "case_type",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            many=True,
            description="Refine facet: filter by case classification.",
        ),
        OpenApiParameter(
            "tags",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            many=True,
            description="Refine facet: filter by keyword/tag.",
        ),
        OpenApiParameter(
            "status",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            many=True,
            description=(
                "Refine facet: coarse case lifecycle (ongoing/closed/others). "
                "Case-scoped in practice."
            ),
        ),
        OpenApiParameter(
            "charge",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            many=True,
            description=(
                "Refine facet: court-case charge, normalized from the case subject "
                "with its descriptive parenthetical removed. Court-scoped in practice."
            ),
        ),
        OpenApiParameter(
            "statute_section",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            many=True,
            description=(
                "Refine facet: statute section cited by a court case, e.g. "
                "'249(3)(ख)'. Digits are normalized to one script."
            ),
        ),
        OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False),
        OpenApiParameter(
            "page_size", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False
        ),
        OpenApiParameter(
            "tags_limit",
            # A raw schema rather than OpenApiTypes.INT so the bounds are
            # MACHINE-readable: a generated client rejects 51 locally instead of
            # learning about it from a 400.
            {"type": "integer", "minimum": 1, "maximum": MAX_TAGS_FACET_SIZE},
            OpenApiParameter.QUERY,
            required=False,
            description=(
                "How many tag buckets the 'tags' facet returns "
                f"(1-{MAX_TAGS_FACET_SIZE}, default {TAGS_FACET_SIZE}). Raise it to "
                "reach the tail of the tag list; it does not affect results, only "
                "the facet."
            ),
        ),
        OpenApiParameter(
            "cursor",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            description=(
                "Opaque deep-paging cursor (the 'next_cursor' from a prior "
                "response). When set, 'page' is ignored and results resume after "
                "the previous page. Use for paging beyond 10,000 results."
            ),
        ),
    ],
    tags=["search"],
)
class UnifiedSearchView(APIView):
    """The single platform-wide search endpoint."""

    permission_classes = [AllowAny]

    def get(self, request):
        serializer = SearchQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        filters = {
            "entity_type": data["entity_type"],
            "case_type": data["case_type"],
            "tags": data["tags"],
            "status": data["status"],
            "charge": data["charge"],
            "statute_section": data["statute_section"],
        }
        active_filters = {k: v for k, v in filters.items() if v}
        # Ephemeral per-response id: it join-keys the server-side analytics event to
        # a future client result-click beacon (query -> shown -> clicked) WITHOUT
        # attaching any identity. Echoed in the envelope so the SPA can send it back.
        search_id = uuid.uuid4().hex
        started = time.perf_counter()
        try:
            response = SearchService().search(
                q=data["q"],
                types=data["type"] or None,
                lang=data["lang"],
                sort=data["sort"],
                filters=active_filters,
                page=data["page"],
                page_size=data["page_size"],
                tags_limit=data["tags_limit"],
                cursor=data.get("cursor"),
            )
        except SearchError as exc:
            # Bad cursor / over-deep offset — a client error, not a 503.
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        except SearchUnavailable as exc:
            # This is a *handled* 503, so neither the Django integration (which only
            # reports unhandled exceptions) nor the service's warning-level log
            # (below Sentry's ERROR event_level) would surface a search-backend
            # outage. Report it explicitly — with the chained transport error — so
            # these 503s are visible and alertable in Sentry. The request/transaction
            # context is inherited from the current scope; ``before_send`` keeps it
            # (a real request has real filenames, not <string>/<stdin>/<console>).
            with sentry_sdk.new_scope() as scope:
                scope.set_tag("search_unavailable", True)
                sentry_sdk.capture_exception(exc)
            return Response(
                {"detail": "Search is temporarily unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        took_ms = (time.perf_counter() - started) * 1000.0
        response["search_id"] = search_id
        # Best-effort product telemetry (never raises); see search/analytics.py.
        emit_search_event(
            search_id=search_id,
            params={
                "q": data["q"],
                "lang": data["lang"],
                "types": data["type"] or None,
                "sort": data["sort"],
                "page": data["page"],
                "page_size": data["page_size"],
                "filters": active_filters,
                # Under a cursor the service ignores ``page`` (stays 1); pass it so
                # the builder doesn't mistake a deep cursor page for the first page.
                "cursor": data.get("cursor"),
            },
            response=response,
            took_ms=took_ms,
        )
        return Response(response)


class SearchClickSerializer(serializers.Serializer):
    """Validates a result-click beacon. ``search_id`` joins back to the
    ``search_query`` event that produced the clicked result list."""

    search_id = serializers.CharField(max_length=64)
    # 1-based position in the full result order (page offset already applied).
    rank = serializers.IntegerField(min_value=1)
    result_type = serializers.ChoiceField(choices=list(ALL_TYPES))
    # The clicked result's public IRI (the envelope ``id``).
    result_id = serializers.CharField(max_length=1024)
    # The relevance score the result was shown with (optional — the label side of
    # the future learning-to-rank signal).
    result_score = serializers.FloatField(required=False)


@extend_schema(
    summary="Search result-click beacon",
    description=(
        "Fire-and-forget beacon recording that a search result was clicked, "
        "join-keyed by 'search_id' to the query that produced it — the other half "
        "of the click loop for future relevance tuning. Public, unauthenticated, "
        "and best-effort: it records NO user identity and always returns 204 (a "
        "beacon cannot read the response). Sent by the SPA via navigator.sendBeacon "
        "(text/plain), so the body is parsed directly rather than via content "
        "negotiation."
    ),
    request=SearchClickSerializer,
    responses={204: None},
    tags=["search"],
)
class SearchClickView(APIView):
    """Ingest a search result-click beacon → one ``search_click`` analytics event."""

    permission_classes = [AllowAny]

    def post(self, request):
        # sendBeacon sends text/plain (CORS-safelisted, no preflight), so read the
        # raw body instead of request.data — DRF would 415 on a non-JSON media type.
        try:
            payload = json.loads(request.body or b"{}")
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            serializer = SearchClickSerializer(data=payload)
            if serializer.is_valid():
                emit_search_click_event(**serializer.validated_data)
        # Always 204: a beacon never reads the response, and a malformed/garbage
        # click is not worth surfacing an error for on best-effort telemetry.
        return Response(status=status.HTTP_204_NO_CONTENT)
