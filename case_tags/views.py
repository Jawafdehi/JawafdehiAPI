"""The vocabulary read endpoint.

It exists because of one line in
``.agents/caseworker/instructions/case-template.md:106``, which tells caseworkers to
"pick from existing tags where possible… or add a new one if needed" — **against a list
nothing has ever exposed**. Told to reuse and given no way to discover, people invent:
144 distinct tags over 82 cases, 97 of them used exactly once.

So this is not a convenience endpoint. It is the missing half of an instruction that has
been in force for months. The tagger reads its enum from here rather than carrying a term
list in a prompt, which would go stale the moment the tagger itself adds a term.
"""

from __future__ import annotations

from typing import Any

from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from case_tags.models import TagAxis
from case_tags.resolve import TagResolver
from case_tags.serializers import TagAxisSerializer, active_terms_by_axis


def _case_counts() -> dict[str, int]:
    """Live usage per canonical term across published cases.

    One pass over published cases, resolving each through a single
    :class:`TagResolver` snapshot. Imported lazily to keep ``case_tags`` from importing
    ``cases`` at module scope — the dependency runs the other way (cases will resolve
    tags at index time), and a cycle here would be awkward to unpick later.

    The count is what lets a reviewer tell an established term from a rarely-used one,
    and it is what makes policy §12's quarterly review possible without a bespoke query.
    """
    from cases.models import Case  # noqa: PLC0415

    resolver = TagResolver()
    counts: dict[str, int] = {}
    for tags in Case.objects.filter(state="PUBLISHED").values_list("tags", flat=True):
        # Per CASE, not per application: a term appearing twice on one case (which the
        # raw data allows) must not read as two cases carrying it.
        for tag_id in resolver.resolve_all([t for t in (tags or []) if isinstance(t, str)]):
            counts[tag_id] = counts.get(tag_id, 0) + 1
    return counts


class VocabularyView(APIView):
    """``GET /api/case-tags/`` — every axis with its terms.

    Public read: the vocabulary is the labelling of a public archive, and the search UI
    needs it to render Nepali facet labels without a second authenticated call.
    """

    permission_classes = [AllowAny]

    @extend_schema(
        summary="The case tag controlled vocabulary, grouped by axis",
        description=(
            "Axes carry their per-case bounds so a client can enforce them without "
            "hardcoding policy. An axis whose `members` is not `enumerated` "
            "legitimately has an empty `terms` list — `institution` and `person` come "
            "from the case entities relation, `geography` from the official "
            "province/district list — and a client must not read that as 'no legal "
            "values'."
        ),
        parameters=[
            OpenApiParameter(
                "axis",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                required=False,
                description="Restrict to one axis id.",
            ),
            OpenApiParameter(
                "include",
                OpenApiTypes.STR,
                OpenApiParameter.QUERY,
                required=False,
                description=(
                    "`all` also returns `proposed` and `merged` terms. Merged terms "
                    "carry `merged_into` so an old stored value stays resolvable."
                ),
            ),
            OpenApiParameter(
                "counts",
                OpenApiTypes.BOOL,
                OpenApiParameter.QUERY,
                required=False,
                description=(
                    "Include live per-term case counts (default true). Pass `false` to "
                    "skip the pass over published cases."
                ),
            ),
        ],
        responses={200: TagAxisSerializer(many=True)},
        tags=["case-tags"],
    )
    def get(self, request: Any) -> Response:
        include_all = request.query_params.get("include") == "all"
        axes = TagAxis.objects.all()
        axis_id = request.query_params.get("axis")
        if axis_id:
            axes = axes.filter(id=axis_id)

        wants_counts = request.query_params.get("counts", "true").lower() not in (
            "false",
            "0",
            "no",
        )
        context = {
            "terms_by_axis": active_terms_by_axis(include_all=include_all),
            "case_counts": _case_counts() if wants_counts else {},
        }
        return Response(TagAxisSerializer(axes, many=True, context=context).data)
