"""T15 — the vocabulary read endpoint. T25 — the review queue API.

The read endpoint exists because of one line in
``.agents/caseworker/instructions/case-template.md:106``, which tells caseworkers to
"pick from existing tags where possible… or add a new one if needed" — **against a list
nothing has ever exposed**. Told to reuse and given no way to discover, people invent:
144 distinct tags over 82 cases, 97 of them used exactly once.

So this is not a convenience endpoint. It is the missing half of an instruction that has
been in force for months, and T14's pickers, T18's classifier and T21's rewritten template
all read from it rather than hardcoding a term list that would drift the moment somebody
approves a proposal.
"""

from __future__ import annotations

from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from jawafdehi_shared.drf.auditlog import AuditlogActorMixin
from review.permissions import CanReadReview, HasContributorRole, IsContentStaff

from case_tags.apply import apply_proposal
from case_tags.models import ProposalStatus, TagAxis, TagProposal
from case_tags.resolve import TagResolver
from case_tags.serializers import (
    TagAxisSerializer,
    TagProposalDecisionSerializer,
    TagProposalPayloadEditSerializer,
    TagProposalSerializer,
    active_terms_by_axis,
)

logger = structlog.get_logger(__name__)


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


class TagProposalViewSet(
    AuditlogActorMixin,
    mixins.CreateModelMixin,
    viewsets.ReadOnlyModelViewSet,
):
    """List / retrieve / create tag proposals, plus edit, approve and reject.

    Permissions mirror :class:`case_proposals.views.CaseUpdateProposalViewSet`, and for
    the same reason: creating is open to any contributor **including the machine role**,
    because automation is the primary producer — but editing and deciding are gated to
    ``IsContentStaff`` (superuser or Caseworker, **not** JobPoller).

    That split is the safety property of this whole app. The alias proposer (T27) files
    hundreds of rows and cannot approve one of them, so no automation can put a term or
    an alias in front of the public on its own authority.
    """

    queryset = TagProposal.objects.all()
    serializer_class = TagProposalSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["status", "kind"]

    def get_permissions(self) -> list[Any]:
        if self.action == "create":
            return [HasContributorRole()]
        if self.action in ("approve", "reject", "edit_payload"):
            return [IsContentStaff()]
        return [CanReadReview()]

    def _reviewer_label(self, request: Any) -> str:
        u = request.user
        handle = getattr(u, "username", "") or getattr(u, "email", "") or str(u.pk)
        return f"caseworker:{handle}"

    def _decide(
        self, request: Any, proposal: TagProposal, new_status: str, apply_first: bool
    ) -> Response:
        decision = TagProposalDecisionSerializer(data=request.data)
        decision.is_valid(raise_exception=True)
        with transaction.atomic():
            # Re-read UNDER A ROW LOCK and check PENDING inside it. Checking the
            # instance get_object() loaded is a time-of-check/time-of-use gap: two
            # concurrent approvals would both pass it and both apply, which for an
            # alias means a unique-violation 500 and for a new term means a duplicate.
            # Whichever request takes the lock second now sees a decided row and 409s.
            proposal = TagProposal.objects.select_for_update().get(pk=proposal.pk)
            if proposal.status != ProposalStatus.PENDING:
                return Response(
                    {
                        "detail": (
                            "Only pending proposals can be decided "
                            f"(is '{proposal.status}')."
                        )
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            reviewer = self._reviewer_label(request)
            if apply_first:
                apply_proposal(proposal.kind, proposal.payload, reviewer)  # 400 on problems
            proposal.status = new_status
            proposal.reviewer = reviewer
            proposal.reviewed_at = timezone.now()
            proposal.review_notes = decision.validated_data.get("notes", "")
            proposal.save(
                update_fields=[
                    "status",
                    "reviewer",
                    "reviewed_at",
                    "review_notes",
                    "updated_at",
                ]
            )
        logger.info(
            "case_tag_proposal.decided",
            decision=new_status,
            proposal_id=proposal.pk,
            kind=proposal.kind,
            dedup_key=proposal.dedup_key,
            acceptor=proposal.reviewer,
            actor_id=getattr(request.user, "pk", None),
            payload=proposal.payload,
        )
        return Response(self.get_serializer(proposal).data)

    @extend_schema(
        request=TagProposalDecisionSerializer,
        responses={200: TagProposalSerializer},
        summary="Approve a tag proposal and apply it to the vocabulary",
        description=(
            "Approving an `alias_equivalence` creates a `TagAlias`; approving a "
            "`new_term` creates an active `Tag`. Neither touches stored `Case.tags` — "
            "aliases are applied when the search document is built, so un-approving "
            "and reindexing fully reverts an approval."
        ),
        tags=["case-tags"],
    )
    @action(detail=True, methods=["post"])
    def approve(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        return self._decide(
            request, self.get_object(), ProposalStatus.APPROVED, apply_first=True
        )

    @extend_schema(
        request=TagProposalDecisionSerializer,
        responses={200: TagProposalSerializer},
        summary="Reject a tag proposal (no change to the vocabulary)",
        description=(
            "The rejection is sticky: `dedup_key` is unique, so the proposer cannot "
            "re-file the same row on its next run. Without that a review queue refills "
            "with refused rows and people stop opening it."
        ),
        tags=["case-tags"],
    )
    @action(detail=True, methods=["post"])
    def reject(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        return self._decide(
            request, self.get_object(), ProposalStatus.REJECTED, apply_first=False
        )

    @extend_schema(
        request=TagProposalPayloadEditSerializer,
        responses={200: TagProposalSerializer},
        summary="Correct a pending proposal's payload before approving it",
        tags=["case-tags"],
    )
    @action(detail=True, methods=["patch"], url_path="payload")
    def edit_payload(self, request: Any, *args: Any, **kwargs: Any) -> Response:
        """Retarget a pending proposal.

        The common case is an alias proposed against a plausible-but-wrong term. Without
        this the reviewer chooses between approving the wrong thing and rejecting it —
        and rejecting is sticky, so the value would silently stay unresolved forever.
        """
        proposal = self.get_object()
        edit = TagProposalPayloadEditSerializer(data=request.data)
        edit.is_valid(raise_exception=True)
        with transaction.atomic():
            proposal = TagProposal.objects.select_for_update().get(pk=proposal.pk)
            if proposal.status != ProposalStatus.PENDING:
                return Response(
                    {"detail": "Only a pending proposal can be edited."},
                    status=status.HTTP_409_CONFLICT,
                )
            # Re-run the kind-aware shape check. ``TagProposalPayloadEditSerializer``
            # only asserts "is an object", so without this a reviewer could save a
            # payload that is missing the fields its own kind requires — producing a row
            # that looks reviewable, sits in the queue, and 400s the moment anyone tries
            # to approve it. The point of the queue is decisions, not litter.
            checked = TagProposalSerializer(
                proposal, data={"payload": edit.validated_data["payload"]}, partial=True
            )
            checked.is_valid(raise_exception=True)
            proposal.payload = checked.validated_data["payload"]
            proposal.save(update_fields=["payload", "updated_at"])
        return Response(self.get_serializer(proposal).data)
