from django_filters.rest_framework import DjangoFilterBackend
from rest_framework import filters, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.views import APIView

from cases.models import Case

from .models import CaseWorkflowRun
from .permissions import IsAdminOrModeratorOrContributorReadOnly
from .registry import get_workflow, list_workflows
from .serializers import (
    CaseWorkflowResumeSerializer,
    CaseWorkflowRunDetailSerializer,
    CaseWorkflowRunSerializer,
)


class CaseWorkflowRunViewSet(viewsets.ReadOnlyModelViewSet):
    """
    List and retrieve CaseWorkflowRun records.

    Accessible by Admin and Moderator (full access), and Contributor (read-only).
    """

    # Auth: inherit the OIDC-only DEFAULT_AUTHENTICATION_CLASSES (no per-view pin).
    permission_classes = [IsAdminOrModeratorOrContributorReadOnly]

    queryset = CaseWorkflowRun.objects.all().order_by("-created_at")
    lookup_field = "run_id"

    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ["workflow_id", "is_complete", "has_failed"]
    search_fields = ["case_id", "run_id"]

    def get_serializer_class(self):
        if self.action == "resume":
            return CaseWorkflowResumeSerializer
        if self.action == "retrieve":
            return CaseWorkflowRunDetailSerializer
        return CaseWorkflowRunSerializer

    @action(detail=True, methods=["post"])
    def resume(self, request, run_id=None):
        """Mark a failed/pending run ready to resume from its failed step."""
        run = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        workflow = get_workflow(run.workflow_id)
        requested_step = serializer.validated_data.get("from_step")
        resume_from_step = requested_step or run.get_resume_step(workflow)

        if not resume_from_step:
            return Response(
                {"error": "No failed or pending step found to resume from"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        can_resume, message = run.can_resume_from(resume_from_step, workflow)
        if not can_resume:
            return Response({"error": message}, status=status.HTTP_400_BAD_REQUEST)

        run.prepare_for_resume(resume_from_step)

        return Response(
            {
                "run_id": run.run_id,
                "case_id": run.case_id,
                "workflow_id": run.workflow_id,
                "status": "ready_for_resume",
                "resume_from_step": resume_from_step,
            }
        )


class EligibleCasesView(APIView):
    """
    Return cases eligible for each registered workflow.

    Response shape::

        [
          {
            "workflow_id": "ciaa_caseworker",
            "display_name": "CIAA Caseworker",
            "cases": [
              {"case_id": "case-abc123", "title": "...", "state": "DRAFT"}
            ]
          }
        ]

    Accessible by Admin and Moderator (full access), and Contributor (read-only).
    """

    # Auth: inherit the OIDC-only DEFAULT_AUTHENTICATION_CLASSES (no per-view pin).
    permission_classes = [IsAdminOrModeratorOrContributorReadOnly]

    def get(self, request):
        workflows = list_workflows()
        result = []

        for workflow in workflows:
            eligible_ids = workflow.get_eligible_cases()

            # Enrich with Case title + state in a single query
            cases_by_id = {
                c["case_id"]: c
                for c in Case.objects.filter(case_id__in=eligible_ids).values(
                    "case_id", "title", "state"
                )
            }

            result.append(
                {
                    "workflow_id": workflow.workflow_id,
                    "display_name": workflow.display_name,
                    "cases": [
                        cases_by_id[cid] for cid in eligible_ids if cid in cases_by_id
                    ],
                }
            )

        return Response(result)
