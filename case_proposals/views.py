import structlog
from django.db import transaction
from django.utils import timezone
from django_filters.rest_framework import DjangoFilterBackend
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from jawafdehi_shared.drf.auditlog import AuditlogActorMixin
from review.permissions import CanReadReview, HasContributorRole, IsContentStaff

from .apply import apply_intent, get_case_or_400
from .models import CaseUpdateProposal, ProposalStatus
from .publish import schedule_decision_event
from .serializers import (
    CaseUpdateProposalSerializer,
    ProposalDecisionSerializer,
    ProposalIntentEditSerializer,
)

logger = structlog.get_logger(__name__)


class CaseUpdateProposalViewSet(
    AuditlogActorMixin,
    mixins.CreateModelMixin,
    viewsets.ReadOnlyModelViewSet,
):
    """List / retrieve / create case-update proposals, plus edit, approve & reject.

    Reads are open to any casework role (ReadOnly or above). Creating a proposal
    is open to any contributor INCLUDING the JobPoller machine role, because
    automation is the primary producer. Editing the proposed change and the
    approve/reject DECISIONS are gated to a human content-staff role
    (``IsContentStaff`` = superuser or Caseworker, and NOT JobPoller) — the system
    is fully human-in-loop for now, so the automation identity must not be able to
    approve (and thereby auto-apply) its own proposals, nor rewrite one after
    filing it. The ``AuditlogActorMixin`` binds the acting user so both the edit
    and the Case write done on approve are attributed to them like any other edit.
    """

    queryset = CaseUpdateProposal.objects.all()
    serializer_class = CaseUpdateProposalSerializer
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ["status", "source_kind", "case_slug"]

    def get_permissions(self):
        if self.action == "create":
            return [HasContributorRole()]
        if self.action in ("approve", "reject", "edit_intent"):
            return [IsContentStaff()]
        return [CanReadReview()]

    def _reviewer_label(self, request):
        u = request.user
        handle = getattr(u, "username", "") or getattr(u, "email", "") or str(u.pk)
        return f"caseworker:{handle}"

    def _decide(self, request, proposal, new_status, apply_first):
        decision = ProposalDecisionSerializer(data=request.data)
        decision.is_valid(raise_exception=True)
        with transaction.atomic():
            # Re-read the proposal UNDER A ROW LOCK and only then check PENDING.
            # The pending check has to live inside the lock: checking the instance
            # get_object() loaded is a time-of-check/time-of-use gap, and two
            # concurrent approvals of the same proposal would both pass it and both
            # apply the intent (a duplicated timeline entry / a twice-applied patch).
            # Locking the Case alone does not close this — it serialises the two
            # approvals but still lets the second one apply. Whichever request gets
            # the lock second now sees APPROVED and 409s.
            proposal = CaseUpdateProposal.objects.select_for_update().get(pk=proposal.pk)
            if proposal.status != ProposalStatus.PENDING:
                return Response(
                    {"detail": f"Only pending proposals can be decided (is '{proposal.status}')."},
                    status=status.HTTP_409_CONFLICT,
                )
            if apply_first:
                case = get_case_or_400(proposal.case_slug)
                apply_intent(case, proposal.intent)  # raises 400 on any problem
            proposal.status = new_status
            proposal.reviewer = self._reviewer_label(request)
            proposal.reviewed_at = timezone.now()
            proposal.review_notes = decision.validated_data.get("notes", "")
            proposal.save(
                update_fields=["status", "reviewer", "reviewed_at", "review_notes", "updated_at"]
            )
            # Announce the decision on the bus once this commits. Registered
            # INSIDE the atomic block so a rollback discards the callback with
            # the transaction; it still fires only after a successful commit.
            # Best-effort and a no-op without NATS_URL — an approval must never
            # depend on a broker being reachable.
            schedule_decision_event(proposal)
        # Audit the decision: who accepted/rejected (the acceptor) + the exact
        # proposed change. The DB LogEntry (register_audited) records the actor
        # on the status transition; this structured line makes the acceptor +
        # intent greppable in one place.
        logger.info(
            "case_proposal.decided",
            decision=new_status,
            proposal_id=proposal.pk,
            case_slug=proposal.case_slug,
            acceptor=proposal.reviewer,
            actor_id=getattr(request.user, "pk", None),
            intent=proposal.intent,
        )
        return Response(self.get_serializer(proposal).data)

    @extend_schema(
        request=ProposalDecisionSerializer,
        responses={200: CaseUpdateProposalSerializer},
        summary="Approve a proposal and apply its intent to the case",
        tags=["case-update-proposals"],
    )
    @action(detail=True, methods=["post"])
    def approve(self, request, *args, **kwargs):
        return self._decide(request, self.get_object(), ProposalStatus.APPROVED, apply_first=True)

    @extend_schema(
        request=ProposalDecisionSerializer,
        responses={200: CaseUpdateProposalSerializer},
        summary="Reject a proposal (no change to the case)",
        tags=["case-update-proposals"],
    )
    @action(detail=True, methods=["post"])
    def reject(self, request, *args, **kwargs):
        return self._decide(request, self.get_object(), ProposalStatus.REJECTED, apply_first=False)

    @extend_schema(
        request=ProposalIntentEditSerializer,
        responses={200: CaseUpdateProposalSerializer},
        summary="Correct a pending proposal's proposed change before approving it",
        tags=["case-update-proposals"],
    )
    @action(detail=True, methods=["patch"], url_path="intent")
    def edit_intent(self, request, *args, **kwargs):
        """Replace the ``intent`` of a PENDING proposal.

        A drafted intent is often nearly right — a misread hearing date, a wrong
        material — and the reviewer should be able to correct it rather than reject
        and wait for a re-proposal. Only the intent changes: provenance still
        describes where the fact came from, and ``confidence`` stays the producer's
        signal (see ``ProposalIntentEditSerializer``).

        A separate step from approve, not a field on it, so the correction lands its
        own audit entry — "who changed the proposed change" is a distinct question
        from "who approved it", and an edited-then-approved proposal must answer both.
        """
        edit = ProposalIntentEditSerializer(data=request.data)
        edit.is_valid(raise_exception=True)
        proposal = self.get_object()
        with transaction.atomic():
            # Same lock discipline as _decide: without re-reading under the lock, an
            # edit could land on a proposal that was approved a moment ago and
            # silently rewrite the record of what was actually applied.
            proposal = CaseUpdateProposal.objects.select_for_update().get(pk=proposal.pk)
            if proposal.status != ProposalStatus.PENDING:
                return Response(
                    {"detail": f"Only pending proposals can be edited (is '{proposal.status}')."},
                    status=status.HTTP_409_CONFLICT,
                )
            before = proposal.intent
            proposal.intent = edit.validated_data["intent"]
            proposal.save(update_fields=["intent", "updated_at"])
        logger.info(
            "case_proposal.intent_edited",
            proposal_id=proposal.pk,
            case_slug=proposal.case_slug,
            editor=self._reviewer_label(request),
            actor_id=getattr(request.user, "pk", None),
            intent_before=before,
            intent_after=proposal.intent,
        )
        return Response(self.get_serializer(proposal).data)
