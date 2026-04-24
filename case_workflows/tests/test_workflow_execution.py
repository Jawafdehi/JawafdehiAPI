"""
Tests for Workflow execution methods (setup_work_dir, execute).
"""

import asyncio
import shutil
from unittest.mock import AsyncMock, patch

import pytest

from case_workflows.models import CaseWorkflowRun
from case_workflows.registry import get_workflow
from case_workflows.workflow import Workflow, WorkflowStep
from cases.models import Case, CaseState, CaseType


@pytest.mark.django_db
class TestWorkflowSetupWorkDir:
    """Tests for Workflow.setup_work_dir()."""

    def _make_case(self, case_id="case-testcase01", court_case="special:081-CR-0001"):
        return Case.objects.get_or_create(
            case_id=case_id,
            defaults={
                "title": f"CIAA Test Case {case_id}",
                "case_type": CaseType.CORRUPTION,
                "state": CaseState.DRAFT,
                "court_cases": [court_case],
            },
        )[0]

    def _make_run(self, case_id="case-testcase01"):
        self._make_case(case_id)
        return CaseWorkflowRun.objects.create(
            case_id=case_id,
            workflow_id="ciaa_caseworker",
        )

    def test_setup_creates_work_dir(self, tmp_path, settings):
        """setup_work_dir creates the work directory."""
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run()

        work_dir = workflow.setup_work_dir(run)

        assert work_dir.is_dir()

        run.refresh_from_db()
        assert run.work_dir == str(work_dir)

        shutil.rmtree(tmp_path, ignore_errors=True)

    def test_setup_calls_workflow_hook(self, tmp_path, settings):
        """on_work_dir_created is called — for CIAA this creates sources/ dirs."""
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run()

        work_dir = workflow.setup_work_dir(run)

        assert (work_dir / "sources" / "raw").is_dir()
        assert (work_dir / "sources" / "markdown").is_dir()
        assert (work_dir / "logs").is_dir()

        shutil.rmtree(tmp_path, ignore_errors=True)

    def test_setup_initialises_case_data(self, tmp_path, settings):
        """setup_work_dir initialises run.case_data with the expected schema."""
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run()

        workflow.setup_work_dir(run)
        run.refresh_from_db()

        assert run.case_data["is_complete"] is False
        assert isinstance(run.case_data["steps"], dict)
        assert isinstance(run.case_data["files"], dict)

        shutil.rmtree(tmp_path, ignore_errors=True)

    def test_setup_work_dir_path_structure(self, tmp_path, settings):
        """Work dir follows <base>/<workflow_id>/<ciaa_court_case_number>/ pattern."""
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        # CIAA workflow uses the Special Court case number (not case_id) as the dir name.
        self._make_case("case-abc123", court_case="special:081-CR-0099")
        run = self._make_run(case_id="case-abc123")

        work_dir = workflow.setup_work_dir(run)

        assert work_dir == tmp_path / "ciaa_caseworker" / "081-CR-0099"

        shutil.rmtree(tmp_path, ignore_errors=True)

    def test_setup_deletes_existing_dir(self, tmp_path, settings):
        """If the work dir already exists it is deleted and recreated."""
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run()

        work_dir1 = workflow.setup_work_dir(run)
        marker = work_dir1 / "marker.txt"
        marker.write_text("hello")

        work_dir2 = workflow.setup_work_dir(run)
        assert work_dir1 == work_dir2
        assert not marker.exists()
        assert (work_dir2 / "sources" / "raw").is_dir()

        shutil.rmtree(tmp_path, ignore_errors=True)

    def test_setup_preserves_existing_dir_when_requested(self, tmp_path, settings):
        """If preserve_existing=True, existing files remain in place."""
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run()

        work_dir = workflow.setup_work_dir(run)
        marker = work_dir / "marker.txt"
        marker.write_text("keep")

        workflow.setup_work_dir(run, preserve_existing=True)
        assert marker.exists()

        shutil.rmtree(tmp_path, ignore_errors=True)

    def test_setup_preserves_existing_case_data(self, tmp_path, settings):
        """If run.case_data already has steps, it is preserved on re-setup."""
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run()

        # Pre-populate case_data as if a step was already done
        existing_data = {
            "is_complete": False,
            "steps": {"initialize-casework": {"status": "complete"}},
            "files": {},
        }
        run.case_data = existing_data
        run.save()

        workflow.setup_work_dir(run)
        run.refresh_from_db()

        # Existing steps should still be there
        assert "initialize-casework" in run.case_data["steps"]

        shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.mark.django_db
class TestWorkflowExecute:
    """Tests for Workflow.execute() — mocks the deep agent to avoid real API calls."""

    def _make_case(self, case_id="case-testexec01", court_case="special:081-CR-0002"):
        return Case.objects.get_or_create(
            case_id=case_id,
            defaults={
                "title": f"CIAA Test Case {case_id}",
                "case_type": CaseType.CORRUPTION,
                "state": CaseState.DRAFT,
                "court_cases": [court_case],
            },
        )[0]

    def _make_run(self, case_id="case-testexec01"):
        self._make_case(case_id)
        return CaseWorkflowRun.objects.create(
            case_id=case_id,
            workflow_id="ciaa_caseworker",
        )

    def test_execute_skips_already_complete_run(self, tmp_path, settings):
        """execute() exits immediately if run.case_data shows is_complete=True."""
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run()
        run.case_data = {"is_complete": True, "steps": {}, "files": {}}
        run.save()

        # Setup work dir so the directory exists
        workflow.setup_work_dir(run)
        # Manually set is_complete back (setup_work_dir preserves it)
        run.case_data = {"is_complete": True, "steps": {}, "files": {}}
        run.save()

        # Patch _execute_async with AsyncMock so asyncio.run can properly await it
        with patch.object(
            workflow, "_execute_async", new_callable=AsyncMock
        ) as mock_async:
            workflow.execute(run, model="openai:gpt-4o")
            mock_async.assert_called_once()

        shutil.rmtree(tmp_path, ignore_errors=True)

    def test_execute_calls_asyncio_run(self, tmp_path, settings):
        """execute() delegates to asyncio.run(_execute_async(...)) with correct args."""
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run()
        workflow.setup_work_dir(run)

        # Patch _execute_async with AsyncMock so asyncio.run can properly await it
        with patch.object(
            workflow, "_execute_async", new_callable=AsyncMock
        ) as mock_async:
            workflow.execute(
                run, model="anthropic:claude-3-5-sonnet", api_key="test-key"
            )
            mock_async.assert_called_once_with(
                run,
                model="anthropic:claude-3-5-sonnet",
                api_key="test-key",
                base_url=None,
                verbose=False,
                recursion_limit=200,
                printer=None,
                resume_from_step=None,
            )

    def test_execute_passes_resume_step(self, tmp_path, settings):
        """execute() passes resume_from_step to async executor."""
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run(case_id="case-resume-pass")
        workflow.setup_work_dir(run)

        with patch.object(
            workflow, "_execute_async", new_callable=AsyncMock
        ) as mock_async:
            workflow.execute(run, resume_from_step="fetch-news-articles")
            mock_async.assert_called_once_with(
                run,
                model="openai:gpt-4o",
                api_key=None,
                base_url=None,
                verbose=False,
                recursion_limit=200,
                printer=None,
                resume_from_step="fetch-news-articles",
            )

    def test_validate_step_outputs_accepts_present_files(self, tmp_path, settings):
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run()
        work_dir = workflow.setup_work_dir(run)
        draft_step = next(s for s in workflow.steps if s.name == "draft-case")

        (work_dir / "draft.md").write_text("x" * 120)

        workflow._validate_step_outputs(work_dir, draft_step)

    def test_validate_step_outputs_rejects_missing_file(self, tmp_path, settings):
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run()
        work_dir = workflow.setup_work_dir(run)
        draft_step = next(s for s in workflow.steps if s.name == "draft-case")

        with pytest.raises(RuntimeError, match="missing draft.md"):
            workflow._validate_step_outputs(work_dir, draft_step)

    def test_execute_marks_run_failed_when_required_output_missing(
        self, tmp_path, settings
    ):
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run()
        case_dir = workflow.setup_work_dir(run)

        class DummyAgent:
            async def ainvoke(self, invocation, config=None):
                return None

            async def astream_events(self, invocation, config=None, version=None):
                if False:
                    yield None

        class DummyClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def get_tools(self):
                return []

        with (
            patch("langchain.chat_models.init_chat_model", return_value=object()),
            patch("langchain_mcp_adapters.client.MultiServerMCPClient", DummyClient),
            patch("deepagents.create_deep_agent", return_value=DummyAgent()),
            patch(
                "case_workflows.workflow.upload_workflow_outputs",
                return_value={},
                create=True,
            ),
            patch.object(run, "mark_started"),
            patch.object(run, "save"),
            patch.object(run, "mark_failed") as mock_mark_failed,
            # get_work_dir does a synchronous DB query inside the async context;
            # patch it to return the already-created directory directly.
            patch.object(workflow, "get_work_dir", return_value=case_dir),
        ):
            with pytest.raises(RuntimeError, match="did not produce required outputs"):
                asyncio.run(
                    workflow._execute_async(
                        run,
                        model="openai:gpt-4o",
                        api_key=None,
                        base_url=None,
                        verbose=False,
                        recursion_limit=20,
                        printer=None,
                    )
                )

        mock_mark_failed.assert_called_once()
        assert "draft-case" in mock_mark_failed.call_args.kwargs["error_message"]

    def test_step_retries_once_and_succeeds_on_second_attempt(self, tmp_path, settings):
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)

        class RetryWorkflow(Workflow):
            @property
            def workflow_id(self) -> str:
                return "retry_workflow"

            @property
            def display_name(self) -> str:
                return "Retry Workflow"

            @property
            def steps(self):
                return [
                    WorkflowStep(
                        name="draft-case",
                        prompt_fn=lambda case_dir: "Draft case",
                        required_outputs={"draft.md": 10},
                        retries=1,
                    )
                ]

            def get_eligible_cases(self):
                return []

            def on_work_dir_created(self, case_dir):
                pass

        workflow = RetryWorkflow()
        run = CaseWorkflowRun.objects.create(
            case_id="case-retry-success",
            workflow_id=workflow.workflow_id,
        )
        work_dir = workflow.setup_work_dir(run)

        class DummyAgent:
            def __init__(self):
                self.calls = 0

            async def ainvoke(self, invocation, config=None):
                self.calls += 1
                if self.calls == 2:
                    (work_dir / "draft.md").write_text("x" * 20)
                return None

            async def astream_events(self, invocation, config=None, version=None):
                if False:
                    yield None

        class DummyClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def get_tools(self):
                return []

        agent = DummyAgent()

        with (
            patch("langchain.chat_models.init_chat_model", return_value=object()),
            patch("langchain_mcp_adapters.client.MultiServerMCPClient", DummyClient),
            patch("deepagents.create_deep_agent", return_value=agent),
            patch("case_workflows.workflow.upload_workflow_outputs", return_value={}),
            patch.object(run, "mark_started"),
            patch.object(run, "save"),
            patch.object(run, "mark_complete") as mock_mark_complete,
            patch.object(run, "mark_failed") as mock_mark_failed,
        ):
            asyncio.run(
                workflow._execute_async(
                    run,
                    model="openai:gpt-4o",
                    api_key=None,
                    base_url=None,
                    verbose=False,
                    recursion_limit=20,
                    printer=None,
                )
            )

        assert agent.calls == 2
        assert run.case_data["steps"]["draft-case"]["status"] == "complete"
        assert len(run.case_data["steps"]["draft-case"]["attempts"]) == 2
        assert run.case_data["steps"]["draft-case"]["attempts"][0]["status"] == "failed"
        assert (
            run.case_data["steps"]["draft-case"]["attempts"][1]["status"] == "complete"
        )
        mock_mark_complete.assert_called_once()
        mock_mark_failed.assert_not_called()

    def test_step_fails_after_retry_budget_exhausted(self, tmp_path, settings):
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)

        class RetryWorkflow(Workflow):
            @property
            def workflow_id(self) -> str:
                return "retry_workflow"

            @property
            def display_name(self) -> str:
                return "Retry Workflow"

            @property
            def steps(self):
                return [
                    WorkflowStep(
                        name="draft-case",
                        prompt_fn=lambda case_dir: "Draft case",
                        required_outputs={"draft.md": 10},
                        retries=1,
                    )
                ]

            def get_eligible_cases(self):
                return []

            def on_work_dir_created(self, case_dir):
                pass

        workflow = RetryWorkflow()
        run = CaseWorkflowRun.objects.create(
            case_id="case-retry-fail",
            workflow_id=workflow.workflow_id,
        )
        workflow.setup_work_dir(run)

        class DummyAgent:
            def __init__(self):
                self.calls = 0

            async def ainvoke(self, invocation, config=None):
                self.calls += 1
                return None

            async def astream_events(self, invocation, config=None, version=None):
                if False:
                    yield None

        class DummyClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def get_tools(self):
                return []

        agent = DummyAgent()

        with (
            patch("langchain.chat_models.init_chat_model", return_value=object()),
            patch("langchain_mcp_adapters.client.MultiServerMCPClient", DummyClient),
            patch("deepagents.create_deep_agent", return_value=agent),
            patch("case_workflows.workflow.upload_workflow_outputs", return_value={}),
            patch.object(run, "mark_started"),
            patch.object(run, "save"),
            patch.object(run, "mark_failed") as mock_mark_failed,
        ):
            with pytest.raises(RuntimeError, match="did not produce required outputs"):
                asyncio.run(
                    workflow._execute_async(
                        run,
                        model="openai:gpt-4o",
                        api_key=None,
                        base_url=None,
                        verbose=False,
                        recursion_limit=20,
                        printer=None,
                    )
                )

        assert agent.calls == 2
        assert run.case_data["steps"]["draft-case"]["status"] == "failed"
        assert len(run.case_data["steps"]["draft-case"]["attempts"]) == 2
        mock_mark_failed.assert_called_once()

    def test_build_step_prompt_adds_retry_fallback_for_draft(self, tmp_path):
        workflow = get_workflow("ciaa_caseworker")
        case_dir = tmp_path / "081-CR-0121"
        draft_step = next(s for s in workflow.steps if s.name == "draft-case")

        attempt1 = workflow._build_step_prompt(
            draft_step,
            case_dir,
            attempt=1,
            max_attempts=2,
        )
        attempt2 = workflow._build_step_prompt(
            draft_step,
            case_dir,
            attempt=2,
            max_attempts=2,
        )

        assert "Retry context" not in attempt1
        assert "Retry context: attempt 2/2." in attempt2
        assert "create" in attempt2.lower()
        assert "draft.md" in attempt2

    def test_build_step_prompt_adds_retry_fallback_for_review(self, tmp_path):
        workflow = get_workflow("ciaa_caseworker")
        case_dir = tmp_path / "081-CR-0121"
        review_step = next(s for s in workflow.steps if s.name == "review-draft")

        attempt1 = workflow._build_step_prompt(
            review_step,
            case_dir,
            attempt=1,
            max_attempts=2,
        )
        attempt2 = workflow._build_step_prompt(
            review_step,
            case_dir,
            attempt=2,
            max_attempts=2,
        )

        assert "Retry context" not in attempt1
        assert "Retry context: attempt 2/2." in attempt2
        assert "draft-review.md" in attempt2
        assert "required output `draft-review.md` was missing" in attempt2
        assert "write a minimally complete review skeleton first" in attempt2.lower()

    def test_review_step_retries_once_and_succeeds_on_second_attempt(
        self, tmp_path, settings
    ):
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)

        class ReviewRetryWorkflow(Workflow):
            @property
            def workflow_id(self) -> str:
                return "review_retry_workflow"

            @property
            def display_name(self) -> str:
                return "Review Retry Workflow"

            @property
            def steps(self):
                return [
                    WorkflowStep(
                        name="review-draft",
                        prompt_fn=lambda case_dir: "Review draft",
                        required_outputs={"draft-review.md": 10},
                        retries=1,
                    )
                ]

            def get_eligible_cases(self):
                return []

            def on_work_dir_created(self, case_dir):
                pass

        workflow = ReviewRetryWorkflow()
        run = CaseWorkflowRun.objects.create(
            case_id="case-review-retry-success",
            workflow_id=workflow.workflow_id,
        )
        work_dir = workflow.setup_work_dir(run)

        class DummyAgent:
            def __init__(self):
                self.calls = 0

            async def ainvoke(self, invocation, config=None):
                self.calls += 1
                if self.calls == 2:
                    (work_dir / "draft-review.md").write_text("x" * 20)
                return None

            async def astream_events(self, invocation, config=None, version=None):
                if False:
                    yield None

        class DummyClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def get_tools(self):
                return []

        agent = DummyAgent()

        with (
            patch("langchain.chat_models.init_chat_model", return_value=object()),
            patch("langchain_mcp_adapters.client.MultiServerMCPClient", DummyClient),
            patch("deepagents.create_deep_agent", return_value=agent),
            patch("case_workflows.workflow.upload_workflow_outputs", return_value={}),
            patch.object(run, "mark_started"),
            patch.object(run, "save"),
            patch.object(run, "mark_complete") as mock_mark_complete,
            patch.object(run, "mark_failed") as mock_mark_failed,
        ):
            asyncio.run(
                workflow._execute_async(
                    run,
                    model="openai:gpt-4o",
                    api_key=None,
                    base_url=None,
                    verbose=False,
                    recursion_limit=20,
                    printer=None,
                )
            )

        assert agent.calls == 2
        assert run.case_data["steps"]["review-draft"]["status"] == "complete"
        assert len(run.case_data["steps"]["review-draft"]["attempts"]) == 2
        assert (
            run.case_data["steps"]["review-draft"]["attempts"][0]["status"] == "failed"
        )
        assert (
            run.case_data["steps"]["review-draft"]["attempts"][1]["status"]
            == "complete"
        )
        mock_mark_complete.assert_called_once()
        mock_mark_failed.assert_not_called()

    def test_review_step_fails_after_retry_budget_exhausted(self, tmp_path, settings):
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)

        class ReviewRetryWorkflow(Workflow):
            @property
            def workflow_id(self) -> str:
                return "review_retry_workflow"

            @property
            def display_name(self) -> str:
                return "Review Retry Workflow"

            @property
            def steps(self):
                return [
                    WorkflowStep(
                        name="review-draft",
                        prompt_fn=lambda case_dir: "Review draft",
                        required_outputs={"draft-review.md": 10},
                        retries=1,
                    )
                ]

            def get_eligible_cases(self):
                return []

            def on_work_dir_created(self, case_dir):
                pass

        workflow = ReviewRetryWorkflow()
        run = CaseWorkflowRun.objects.create(
            case_id="case-review-retry-fail",
            workflow_id=workflow.workflow_id,
        )
        workflow.setup_work_dir(run)

        class DummyAgent:
            def __init__(self):
                self.calls = 0

            async def ainvoke(self, invocation, config=None):
                self.calls += 1
                return None

            async def astream_events(self, invocation, config=None, version=None):
                if False:
                    yield None

        class DummyClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def get_tools(self):
                return []

        agent = DummyAgent()

        with (
            patch("langchain.chat_models.init_chat_model", return_value=object()),
            patch("langchain_mcp_adapters.client.MultiServerMCPClient", DummyClient),
            patch("deepagents.create_deep_agent", return_value=agent),
            patch("case_workflows.workflow.upload_workflow_outputs", return_value={}),
            patch.object(run, "mark_started"),
            patch.object(run, "save"),
            patch.object(run, "mark_failed") as mock_mark_failed,
        ):
            with pytest.raises(RuntimeError, match="did not produce required outputs"):
                asyncio.run(
                    workflow._execute_async(
                        run,
                        model="openai:gpt-4o",
                        api_key=None,
                        base_url=None,
                        verbose=False,
                        recursion_limit=20,
                        printer=None,
                    )
                )

        assert agent.calls == 2
        assert run.case_data["steps"]["review-draft"]["status"] == "failed"
        assert len(run.case_data["steps"]["review-draft"]["attempts"]) == 2
        mock_mark_failed.assert_called_once()

    def test_validate_draft_inputs_warns_for_news_over_cap_without_override(
        self, tmp_path, settings, caplog
    ):
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run(case_id="081-CR-0121")
        work_dir = workflow.setup_work_dir(run)

        markdown_dir = work_dir / "sources" / "markdown"
        for idx in range(1, 12):
            (markdown_dir / f"news-{idx}.md").write_text("news", encoding="utf-8")

        with caplog.at_level("WARNING"):
            workflow._validate_draft_inputs(work_dir)

        assert "draft-case input policy warning" in caplog.text
        assert "11 news markdown files (max 10)" in caplog.text
        assert "no documented override reason" in caplog.text
        assert "Drafting will continue." in caplog.text

    def test_validate_draft_inputs_warns_for_news_over_cap_with_override(
        self, tmp_path, settings, caplog
    ):
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run(case_id="081-CR-0121")
        work_dir = workflow.setup_work_dir(run)

        markdown_dir = work_dir / "sources" / "markdown"
        for idx in range(1, 12):
            (markdown_dir / f"news-{idx}.md").write_text("news", encoding="utf-8")

        logs_dir = work_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        (logs_dir / "news-search-summary.md").write_text(
            "Override reason: source diversity required for conflicting verdict coverage.",
            encoding="utf-8",
        )

        with caplog.at_level("WARNING"):
            workflow._validate_draft_inputs(work_dir)

        assert "draft-case input policy warning" in caplog.text
        assert "11 news markdown files (max 10)" in caplog.text
        assert "documented override reason" in caplog.text
        assert "no documented override reason" not in caplog.text
        assert "Drafting will continue." in caplog.text

    def test_validate_draft_inputs_rejects_escaped_unicode_heavy_sources(
        self, tmp_path, settings
    ):
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run(case_id="081-CR-0121")
        work_dir = workflow.setup_work_dir(run)

        markdown_dir = work_dir / "sources" / "markdown"
        escaped_blob = " ".join(["u0915"] * 30)
        (markdown_dir / "news-bad.md").write_text(escaped_blob, encoding="utf-8")

        with pytest.raises(RuntimeError, match="escaped-unicode-heavy"):
            workflow._validate_draft_inputs(work_dir)

    def test_validate_draft_inputs_handles_invalid_utf8_in_source_markdown(
        self, tmp_path, settings
    ):
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run(case_id="081-CR-0121")
        work_dir = workflow.setup_work_dir(run)

        markdown_dir = work_dir / "sources" / "markdown"
        bad_file = markdown_dir / "news-bad-bytes.md"
        bad_file.write_bytes(b"prefix " + bytes([0xBE]) + b" suffix")

        # Should not raise decode errors; resilient reader must recover.
        workflow._validate_draft_inputs(work_dir)

    def test_validate_draft_inputs_handles_invalid_utf8_in_news_summary(
        self, tmp_path, settings
    ):
        settings.CASE_WORKFLOWS_WORK_DIR = str(tmp_path)
        workflow = get_workflow("ciaa_caseworker")
        run = self._make_run(case_id="081-CR-0121")
        work_dir = workflow.setup_work_dir(run)

        markdown_dir = work_dir / "sources" / "markdown"
        for idx in range(1, 12):
            (markdown_dir / f"news-{idx}.md").write_text("news", encoding="utf-8")

        logs_dir = work_dir / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        summary_path = logs_dir / "news-search-summary.md"
        summary_path.write_bytes(b"Override reason: " + bytes([0xBE]))

        # Override token is still present; decode should be resilient.
        workflow._validate_draft_inputs(work_dir)
