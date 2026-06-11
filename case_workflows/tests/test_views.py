import pytest
from rest_framework.test import APIClient

from case_workflows.models import CaseWorkflowRun
from cases.models import Case
from tests.conftest import create_user_with_role


@pytest.mark.django_db
class TestCaseWorkflowViews:
    def test_resume_endpoint_marks_run_ready(self):
        user = create_user_with_role(
            username="resume_admin",
            email="resume_admin@example.com",
            role="Admin",
        )
        client = APIClient()
        client.force_authenticate(user=user)

        Case.objects.create(case_id="case-view-01", title="Test case", state="DRAFT")
        run = CaseWorkflowRun.objects.create(
            case_id="case-view-01",
            workflow_id="ciaa_caseworker",
            has_failed=True,
            error_message="step failed",
            case_data={
                "is_complete": False,
                "steps": {
                    "initialize-casework": {"status": "complete"},
                    "fetch-source-documents": {"status": "failed"},
                },
                "files": {},
            },
        )

        response = client.post(
            f"/api/case-workflows/runs/{run.run_id}/resume/",
            {},
            format="json",
        )

        assert response.status_code == 200
        assert response.data["status"] == "ready_for_resume"
        assert response.data["resume_from_step"] == "fetch-source-documents"

        run.refresh_from_db()
        assert run.has_failed is False
        assert run.error_message == ""

    def test_resume_endpoint_rejects_complete_run(self):
        user = create_user_with_role(
            username="resume_admin_2",
            email="resume_admin_2@example.com",
            role="Admin",
        )
        client = APIClient()
        client.force_authenticate(user=user)

        Case.objects.create(case_id="case-view-02", title="Test case", state="DRAFT")
        run = CaseWorkflowRun.objects.create(
            case_id="case-view-02",
            workflow_id="ciaa_caseworker",
            is_complete=True,
            case_data={"is_complete": True, "steps": {}, "files": {}},
        )

        response = client.post(
            f"/api/case-workflows/runs/{run.run_id}/resume/",
            {},
            format="json",
        )

        assert response.status_code == 400
        assert "already complete" in response.data["error"]
