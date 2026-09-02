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

from courts.geography import ALL_COURT_IDENTIFIERS

from .analytics import emit_search_click_event, emit_search_event
from .service import (
    ALL_COURT_TYPES,
    ALL_SORTS,
    ALL_TYPES,
    FACET_FIELDS,
    MAX_FACET_Q_TEXT,
    MAX_PAGE_SIZE,
    RANGE_FIELDS,
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
    # ONE-court refine facet (NGM court cases only) — the deciding court's
    # identifier, repeatable, so an arbitrary set of courts is selectable
    # ("kathmandudc" + "patanhc" + "supreme"). That set is what the coarse
    # court_type/district pair below CANNOT express: those AND together, so
    # asking for two tiers and two districts returns the cross-product.
    #
    # CLOSED, against courts.geography — the same table the indexer resolves
    # geography from, and the scraper's own registry, so a court missing from it
    # is a court nothing scrapes and therefore one with no cases to exclude. A
    # typo is a 400, not a confident empty page.
    court = serializers.ListField(
        child=serializers.ChoiceField(choices=sorted(ALL_COURT_IDENTIFIERS)),
        required=False,
        default=list,
    )
    # Court tier refine facet (NGM court cases only). Also a CLOSED vocabulary,
    # and structurally stable: these are the constitutional tiers, and they are
    # the only four values ``Court.court_type`` holds. Sourced from
    # ``ALL_COURT_TYPES`` so this, the OpenAPI enum below and the MCP tool's
    # schema cannot drift into three different answers.
    court_type = serializers.ListField(
        child=serializers.ChoiceField(choices=list(ALL_COURT_TYPES)),
        required=False,
        default=list,
    )
    # Court geography (NGM court cases only). Free-text like ``tags``: the
    # canonical values are whatever the ``facets.district``/``facets.province``
    # buckets return (title-case English names, plus the NATIONAL sentinel on
    # province for supreme/special) — a closed 77-way ChoiceField would be
    # brittle. ``district`` is a DISTRICT COURT's own district and matches
    # nothing else; high/supreme/special are reachable via province.
    district = serializers.ListField(
        child=serializers.CharField(allow_blank=False), required=False, default=list
    )
    province = serializers.ListField(
        child=serializers.CharField(allow_blank=False), required=False, default=list
    )
    # Facet-VALUE search: ``facet_q=<facet>:<text>`` recomputes only the named
    # facet's bucket list to the buckets whose key contains <text> (matched over
    # the full aggregation), without affecting results, count, or any other
    # facet. Repeatable — once per facet. Parsed to {facet: text} below.
    facet_q = serializers.ListField(
        child=serializers.CharField(allow_blank=False), required=False, default=list
    )

    def validate_facet_q(self, value):
        queries: dict[str, str] = {}
        for item in value:
            facet, sep, text = item.partition(":")
            # Strip the TEXT half. DRF's CharField trims the whole ITEM, not the
            # part after the colon, so the natural ``?facet_q=tags: घुस`` would
            # otherwise carry a leading space into the include regex
            # (``.* घुस.*``) and match no bucket at all — a silently empty facet
            # rather than an error, which is the one outcome this endpoint tries
            # hardest to avoid.
            #
            # Only INTERIOR-leading whitespace can reach here: a trailing-space
            # item is trimmed by the child field, so whitespace-only text has
            # already collapsed to ``tags:`` and 400d on the shape check below
            # before this line runs.
            #
            # The FACET half is left strict on purpose: ``?facet_q=tags :x``
            # already fails loudly, with the legal facet names in the message.
            text = text.strip()
            if not sep or not facet or not text:
                raise serializers.ValidationError(
                    f"facet_q must be '<facet>:<text>', got {item!r}."
                )
            if facet not in FACET_FIELDS:
                raise serializers.ValidationError(
                    f"unknown facet {facet!r}; one of {sorted(FACET_FIELDS)}."
                )
            if facet in queries:
                raise serializers.ValidationError(
                    f"facet_q given twice for {facet!r}."
                )
            # Bound the TEXT, not the whole item: it is the only half interpolated
            # into the cluster-side ``include`` regex, and it is what the automaton
            # cost scales with. Unbounded, a long value compiles to a pattern
            # Lucene refuses to determinize, and the shard error comes back as a
            # 503 + Sentry outage event rather than the 400 this plainly is.
            if len(text) > MAX_FACET_Q_TEXT:
                raise serializers.ValidationError(
                    f"facet_q text for {facet!r} is {len(text)} characters; "
                    f"the maximum is {MAX_FACET_Q_TEXT}."
                )
            queries[facet] = text
        return queries
    # बिगो (alleged embezzled amount, whole NPR) range bounds — the first NON
    # exact-match refine control. Inclusive on both sides (gte/lte).
    #
    # No default: an absent bound must stay ABSENT from validated_data so the view
    # can tell "not requested" from a real ``0`` lower bound.
    #
    # ``max_value`` is the signed-64-bit ceiling, matching both the ``long`` index
    # mapping and ``Case.bigo``'s BigIntegerField. Without it a larger integer
    # reaches OpenSearch and comes back as a number_format_exception — i.e. a 503
    # for what is plainly a bad request. ``min_value=0`` for the same reason in
    # reverse: a negative बिगो is not a thing, so it is a client error, not an
    # empty result set.
    bigo_min = serializers.IntegerField(required=False, min_value=0, max_value=2**63 - 1)
    bigo_max = serializers.IntegerField(required=False, min_value=0, max_value=2**63 - 1)
    # Gregorian date-range bounds over the shared indexed ``date`` field —
    # inclusive on both sides, like the बिगो pair above, and with the same
    # no-default rule: an absent bound must stay ABSENT from validated_data.
    # ``DateField`` supplies the 400 on garbage ("2024-13-45", "abc").
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)

    def validate(self, attrs):
        # An inverted interval matches nothing. Say so with a 400 rather than
        # serving a confident, empty, indistinguishable-from-"no such cases" page.
        low, high = attrs.get("bigo_min"), attrs.get("bigo_max")
        if low is not None and high is not None and low > high:
            raise serializers.ValidationError(
                "bigo_min must be less than or equal to bigo_max."
            )
        # Same rule for the date interval (comparison happens while the values are
        # still ``datetime.date`` objects, so it is calendar-correct).
        start, end = attrs.get("date_from"), attrs.get("date_to")
        if start is not None and end is not None and start > end:
            raise serializers.ValidationError(
                "date_from must be on or before date_to."
            )
        # Re-serialize to ISO strings: everything downstream expects pure-JSON
        # validated_data — build_query's DSL is contract-tested by byte-stable
        # dict equality, and the analytics event dict is JSON-logged. A
        # ``datetime.date`` would be at the mercy of each consumer's serializer.
        for key in ("date_from", "date_to"):
            if attrs.get(key) is not None:
                attrs[key] = attrs[key].isoformat()
        return attrs

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
        "documents. Backed by OpenSearch — returns 503 if the cluster is down.\n\n"
        "Roman-script query tokens of four characters or more also get BOUNDED "
        "fuzzy matching (at most two edits), so a misspelled romanization such as "
        "'coruption' still reaches 'corruption'. It is a damped last-resort route: "
        "a fuzzy hit always ranks below a correctly spelled one, and Devanagari, "
        "case numbers and numeric tokens are matched exactly as before.\n\n"
        "The envelope's 'did_you_mean' carries a single suggested spelling (drawn "
        "from curated tags and indexed title romanizations) or null. It is offered "
        "when the search returned nothing, and also when it returned only fuzzy "
        "matches — i.e. nothing on the page matched the query as typed, so the "
        "results have no exactly-matching anchor. A correctly spelled query that "
        "found real matches never carries one. The key is always present, and the "
        "suggestion is never applied automatically — re-search only if the reader "
        "selects it."
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
            "court",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            many=True,
            enum=sorted(ALL_COURT_IDENTIFIERS),
            description=(
                "Refine facet: the deciding court's identifier — one of the 97 "
                "courts (77 district, 18 high, supreme, special); list them with "
                "GET /api/courts/. Repeatable, so an arbitrary SET of courts is "
                "selectable (?court=kathmandudc&court=patanhc); court_type and "
                "district AND together and so cannot express one. "
                "COURT-CASE-SCOPED: only NGM court cases carry a court, so any "
                "value also excludes every entity, material and Jawafdehi-case "
                "result — pair it with ?type=courtcase. Inert until the "
                "court-case index is rebuilt (reindex_courtcases --rebuild) "
                "after this field shipped."
            ),
        ),
        OpenApiParameter(
            "court_type",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            many=True,
            enum=list(ALL_COURT_TYPES),
            description=(
                "Refine facet: court tier. Same court-case scoping and rebuild "
                "caveat as court. Note the response's extra.court is the court "
                "IDENTIFIER, not this tier."
            ),
        ),
        OpenApiParameter(
            "district",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            many=True,
            description=(
                "Refine facet: a DISTRICT COURT's own district — the canonical "
                "English names returned in facets.district (e.g. 'Kathmandu'). "
                "Matches district-court cases ONLY: a high court is a provincial "
                "court and carries no district (use ?province=), and "
                "supreme/special carry none either. So "
                "?court_type=district&district=Kathmandu is exactly Kathmandu "
                "District Court. Same court-case scoping and rebuild caveat as "
                "court."
            ),
        ),
        OpenApiParameter(
            "province",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            many=True,
            description=(
                "Refine facet: the court's province (one of the 7, e.g. "
                "'Bagmati') — set for all 95 sub-national courts, a high court "
                "resolving to the province it serves (its additional benches "
                "included). 'NATIONAL' selects supreme + special-court cases. "
                "Same court-case scoping and rebuild caveat as court."
            ),
        ),
        OpenApiParameter(
            "bigo_min",
            OpenApiTypes.INT,
            OpenApiParameter.QUERY,
            required=False,
            description=(
                "Refine filter: minimum बिगो — the alleged embezzled/disputed "
                "amount, in whole NPR (inclusive). CASE-SCOPED: only Jawafdehi "
                "cases carry an amount, so any bigo bound also excludes every "
                "entity, material and court-case result — pair it with "
                "?type=case. Cases with no recorded amount are excluded too."
            ),
        ),
        OpenApiParameter(
            "bigo_max",
            OpenApiTypes.INT,
            OpenApiParameter.QUERY,
            required=False,
            description=(
                "Refine filter: maximum बिगो in whole NPR (inclusive). Same "
                "case-scoping caveat as bigo_min. A bigo_min greater than "
                "bigo_max is rejected with 400 rather than returning nothing."
            ),
        ),
        OpenApiParameter(
            "date_from",
            OpenApiTypes.DATE,
            OpenApiParameter.QUERY,
            required=False,
            description=(
                "Refine filter: earliest record date, Gregorian YYYY-MM-DD "
                "(inclusive). Filters the shared indexed ``date`` — a case's "
                "registration date, a material's publication date, a Jawafdehi "
                "case's start date. Entities carry NO date, so any bound "
                "excludes every entity result — pair it with ?type=. Documents "
                "with no recorded date are excluded too."
            ),
        ),
        OpenApiParameter(
            "date_to",
            OpenApiTypes.DATE,
            OpenApiParameter.QUERY,
            required=False,
            description=(
                "Refine filter: latest record date, Gregorian YYYY-MM-DD "
                "(inclusive). Same entity-scoping caveat as date_from. A "
                "date_from after date_to is rejected with 400 rather than "
                "returning nothing."
            ),
        ),
        OpenApiParameter(
            "facet_q",
            OpenApiTypes.STR,
            OpenApiParameter.QUERY,
            required=False,
            many=True,
            description=(
                "Facet-value search: '<facet>:<text>' (e.g. 'tags:\u0918\u0941\u0938') "
                "recomputes ONLY the named facet's bucket list to the top "
                "buckets whose key contains <text>, case-insensitively, matched "
                "over the full aggregation rather than only the buckets that fit "
                "the facet's own bucket limit. Results, count, and every other "
                "facet are unaffected. "
                "Repeatable, once per facet; <text> is treated literally "
                "(regex-escaped server-side) and is limited to "
                f"{MAX_FACET_Q_TEXT} characters."
            ),
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
        # Driven off FACET_FIELDS (params share serializer field names), so a new
        # facet reaches the service without editing a hand-list here — the same
        # registry discipline as ``active_ranges`` below, with the same limit:
        # a FACET_FIELDS entry still needs its serializer field above, pinned by
        # ``test_every_facet_field_has_an_agg_and_a_serializer_field``.
        active_filters = {
            param: values for param in FACET_FIELDS if (values := data[param])
        }
        # Range bounds are kept SEPARATE from the exact-match facets: they are a
        # different clause kind (``range`` vs ``terms``) and a different value shape
        # (a scalar, not a list). Emptiness is ``is None`` — a truthiness test would
        # drop a legitimate ``bigo_min=0``.
        # Driven off RANGE_FIELDS rather than a hand-listed pair, so a new bound
        # reaches the service without editing this comprehension. Note the limit:
        # it reads ``validated_data``, so a RANGE_FIELDS entry with no serializer
        # field above resolves to None forever and the bound is silently dropped.
        # Both halves are required — pinned by
        # ``test_every_range_field_is_declared_on_the_query_serializer``.
        active_ranges = {
            param: value
            for param in RANGE_FIELDS
            if (value := data.get(param)) is not None
        }
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
                ranges=active_ranges,
                facet_queries=data["facet_q"] or None,
                page=data["page"],
                page_size=data["page_size"],
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
                "ranges": active_ranges,
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
