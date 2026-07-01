"""The unified platform search API: ``GET /api/search/``.

Public read. One query across entities, materials, court cases, and PUBLISHED
cases (the index is all-public — no ACL filter). OpenSearch is a hard dependency:
if the cluster is unreachable the endpoint returns 503 (no in-process fallback).

This REPLACES the old Jawafdehi-scoped ``cases.UnifiedSearchView`` and the NGM
501 search stub.
"""

from __future__ import annotations

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .service import (
    ALL_SORTS,
    ALL_TYPES,
    MAX_PAGE_SIZE,
    SORT_RELEVANCE,
    SearchError,
    SearchService,
    SearchUnavailable,
)


class SearchQuerySerializer(serializers.Serializer):
    # ``q`` is OPTIONAL: an empty/absent query becomes a browse (match-all) so the
    # endpoint can list/page the corpus and apply facet filters/sort without a
    # search term (e.g. "all entities of type X, newest first").
    q = serializers.CharField(required=False, allow_blank=True, default="")
    type = serializers.ListField(
        child=serializers.ChoiceField(choices=list(ALL_TYPES)),
        required=False,
        default=list,
    )
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
    page = serializers.IntegerField(required=False, min_value=1, default=1)
    page_size = serializers.IntegerField(
        required=False, min_value=1, max_value=MAX_PAGE_SIZE, default=10
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
        OpenApiParameter("page", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False),
        OpenApiParameter(
            "page_size", OpenApiTypes.INT, OpenApiParameter.QUERY, required=False
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
        }
        try:
            response = SearchService().search(
                q=data["q"],
                types=data["type"] or None,
                lang=data["lang"],
                sort=data["sort"],
                filters={k: v for k, v in filters.items() if v},
                page=data["page"],
                page_size=data["page_size"],
                cursor=data.get("cursor"),
            )
        except SearchError as exc:
            # Bad cursor / over-deep offset — a client error, not a 503.
            return Response(
                {"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST
            )
        except SearchUnavailable:
            return Response(
                {"detail": "Search is temporarily unavailable."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(response)
