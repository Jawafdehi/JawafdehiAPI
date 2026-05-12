"""
Management command to run case workflows.

Usage::

    # List all registered workflows
    python manage.py run_case_workflow --list

    # Run workflow for all eligible cases
    python manage.py run_case_workflow ciaa_caseworker

    # Run workflow for a specific case
    python manage.py run_case_workflow ciaa_caseworker --case-id case-abc123

    # Run workflow in update-existing mode (patch existing draft cases)
    python manage.py run_case_workflow ciaa_caseworker --update-existing
    python manage.py run_case_workflow ciaa_caseworker --update-existing --case-id case-abc123

    # Use a specific model (any OpenAI-compatible provider)
    python manage.py run_case_workflow ciaa_caseworker --model openai:gpt-4o
    python manage.py run_case_workflow ciaa_caseworker --model anthropic:claude-sonnet-4-5

Environment variables (used as defaults when flags are not passed)::

    JAWAFDEHI_CASEWORK_MODEL    — default model (e.g. openai:gpt-4o)
    JAWAFDEHI_CASEWORK_API_KEY  — API key for the LLM provider
    JAWAFDEHI_CASEWORK_BASE_URL — base URL for OpenAI-compatible endpoints
"""

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from case_workflows.models import CaseWorkflowRun
from case_workflows.output import WorkflowPrinter
from case_workflows.registry import get_workflow, list_workflows


def _format_error(exc: Exception) -> str:
    """
    Return a human-friendly error string.
    Detects 429 rate-limit errors and surfaces the retry delay prominently.
    """
    msg = str(exc)
    # 429 RESOURCE_EXHAUSTED — present in openai, langchain-google-genai, anthropic errors
    if (
        "429" in msg
        or "RESOURCE_EXHAUSTED" in msg
        or "rate_limit" in msg.lower()
        or "RateLimitError" in type(exc).__name__
    ):
        retry_match = re.search(
            r"(?:retry[_ ]?in|retryDelay)[^0-9]*([0-9]+h[0-9]+m[0-9.]+s|[0-9]+m[0-9.]+s|[0-9]+s)",
            msg,
            re.IGNORECASE,
        )
        retry_hint = f" Retry in: {retry_match.group(1)}" if retry_match else ""
        # Extract quota metric name if present
        quota_match = re.search(r"generativelanguage\.googleapis\.com/([\w/]+)", msg)
        quota_hint = f" (quota: {quota_match.group(1)})" if quota_match else ""
        return (
            f"Rate limit exceeded — you have hit the API quota.{quota_hint}{retry_hint}"
        )
    return msg


class Command(BaseCommand):
    help = "Run a registered case workflow against eligible cases"

    def add_arguments(self, parser):
        parser.add_argument(
            "workflow_id",
            nargs="?",
            type=str,
            help="Workflow template ID to run (e.g. ciaa_caseworker)",
        )
        parser.add_argument(
            "--case-id",
            type=str,
            help="Target a specific Jawafdehi case_id instead of auto-selecting",
        )
        parser.add_argument(
            "--model",
            type=str,
            default=None,
            help=(
                "LLM model to use, in provider:model format. "
                "Supports any OpenAI-compatible provider "
                "(openai, anthropic, google, ollama, etc.). "
                "Env: JAWAFDEHI_CASEWORK_MODEL (fallback: openai:gpt-4o)."
            ),
        )
        parser.add_argument(
            "--api-key",
            type=str,
            default=None,
            help=(
                "API key for the LLM provider. "
                "Env: JAWAFDEHI_CASEWORK_API_KEY. "
                "Falls back to the provider's standard env var "
                "(e.g. OPENAI_API_KEY, ANTHROPIC_API_KEY) if not set."
            ),
        )
        parser.add_argument(
            "--base-url",
            type=str,
            default=None,
            help=(
                "Base URL for OpenAI-compatible endpoints "
                "(e.g. http://localhost:11434/v1 for Ollama). "
                "Env: JAWAFDEHI_CASEWORK_BASE_URL."
            ),
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Stream agent events (tool calls, model output) to stdout.",
        )
        parser.add_argument(
            "--recursion-limit",
            type=int,
            default=200,
            dest="recursion_limit",
            help="LangGraph recursion limit per step (default: 200).",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            dest="list_workflows",
            help="List all registered workflows and exit",
        )
        parser.add_argument(
            "--resume",
            action="store_true",
            help="Resume an existing failed run from its failed step.",
        )
        parser.add_argument(
            "--resume-from-step",
            type=str,
            default=None,
            help="Override the step name to resume from (defaults to failed step).",
        )
        parser.add_argument(
            "--update-existing",
            action="store_true",
            dest="update_existing",
            help=(
                "Run in update mode: patch existing draft cases instead of "
                "creating new ones. Steps 5-7 find and patch the existing "
                "Jawafdehi case."
            ),
        )

    def handle(self, *args, **options):
        printer = WorkflowPrinter()

        # ---- List workflows mode ----
        if options["list_workflows"]:
            wids = list_workflows()
            if not wids:
                printer.warn("No workflows registered.")
                return
            printer._console.print("Registered workflows:")
            for wid in wids:
                wf = get_workflow(wid)
                printer._console.print(f"  • [bold]{wid}[/bold] — {wf.display_name}")
            return

        # ---- Require workflow_id for execution ----
        workflow_id = options["workflow_id"]
        if not workflow_id:
            raise CommandError(
                "Provide a workflow_id, or use --list to see available workflows."
            )

        try:
            workflow = get_workflow(workflow_id)
        except KeyError as exc:
            raise CommandError(str(exc))

        model = options["model"] or os.environ.get(
            "JAWAFDEHI_CASEWORK_MODEL", "openai:gpt-4o"
        )
        api_key = options["api_key"] or os.environ.get("JAWAFDEHI_CASEWORK_API_KEY")
        base_url = options["base_url"] or os.environ.get("JAWAFDEHI_CASEWORK_BASE_URL")
        verbose = options["verbose"]
        recursion_limit = options["recursion_limit"]

        if verbose:
            printer.install_logging_handler()

        printer.print_workflow_header(
            workflow.display_name, workflow_id, model, base_url
        )
        if update_existing:
            printer._console.print(
                "  [bold yellow]Mode: update-existing[/bold yellow]"
            )

        # ---- Determine target cases ----
        specific_case = options["case_id"]
        resume = options["resume"]
        resume_from_step_override = options["resume_from_step"]

        if resume and not specific_case:
            raise CommandError("--resume requires --case-id")

        if resume_from_step_override and not resume:
            raise CommandError("--resume-from-step can only be used with --resume")

        update_existing = options["update_existing"]
        if update_existing:
            workflow.update_existing = True

        if specific_case:
            case_ids = [specific_case]
            printer._console.print(
                f"  Targeting specific case: [bold]{specific_case}[/bold]"
            )
        else:
            case_ids = workflow.get_eligible_cases()
            printer._console.print(
                f"  Found [bold]{len(case_ids)}[/bold] eligible case(s)"
            )

        if not case_ids:
            printer.warn("No cases to process. Exiting.")
            return

        # ---- Execute workflow for each case ----
        success_count = 0
        skip_count = 0
        fail_count = 0

        target_runs: list[tuple[CaseWorkflowRun, bool]] = []
        for cid in case_ids:
            if resume:
                try:
                    run = CaseWorkflowRun.objects.get(
                        case_id=cid,
                        workflow_id=workflow_id,
                    )
                    created = False
                except CaseWorkflowRun.DoesNotExist as exc:
                    raise CommandError(
                        f"Cannot resume: no existing run for case '{cid}' and workflow '{workflow_id}'"
                    ) from exc
            else:
                run, created = CaseWorkflowRun.objects.get_or_create(
                    case_id=cid,
                    workflow_id=workflow_id,
                )
            target_runs.append((run, created))

        for run, created in target_runs:
            cid = run.case_id

            if not created and run.is_complete and not update_existing:
                printer.print_step_skipped(cid)
                skip_count += 1
                continue

            if (
                not created
                and run.started_at is not None
                and not run.is_complete
                and not run.has_failed
            ):
                printer.warn(
                    f"Skipping {cid}: run is already in progress "
                    f"(started {run.started_at.isoformat()}). "
                    "Use --resume to retry a failed run."
                )
                skip_count += 1
                continue

            printer.print_case_header(cid, created)

            if update_existing and not created and run.is_complete:
                run.is_complete = False
                run.has_failed = False
                run.error_message = ""
                run.case_data = {}
                run.save(
                    update_fields=[
                        "is_complete", "has_failed", "error_message",
                        "case_data", "updated_at",
                    ]
                )

            try:
                resume_from_step = None
                preserve_existing = False
                if resume:
                    resume_from_step = resume_from_step_override or run.get_resume_step(
                        workflow
                    )
                    if not resume_from_step:
                        raise CommandError(
                            "Run is not resumable because no failed or pending step was found"
                        )
                    can_resume, message = run.can_resume_from(
                        resume_from_step, workflow
                    )
                    if not can_resume:
                        raise CommandError(message)
                    run.prepare_for_resume(resume_from_step)
                    preserve_existing = True
                    printer.warn(f"Resuming from step: {resume_from_step}")

                workflow.setup_work_dir(run, preserve_existing=preserve_existing)
                printer.print_work_dir(run.work_dir)
                log_path = (
                    Path(run.work_dir)
                    / "logs"
                    / f"run-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%S')}.log"
                )
                printer.enable_file_logging(log_path)
                try:
                    rel_log = log_path.relative_to(Path.cwd())
                except ValueError:
                    rel_log = log_path
                printer._console.print(f"  [dim]📝 Log: {rel_log}[/dim]")
                workflow.execute(
                    run,
                    model=model,
                    api_key=api_key,
                    base_url=base_url,
                    verbose=verbose,
                    recursion_limit=recursion_limit,
                    printer=printer,
                    resume_from_step=resume_from_step,
                )
            except KeyboardInterrupt:
                printer.error("Interrupted — stopping")
                fail_count += 1
                break
            except Exception as exc:
                printer.error(_format_error(exc))
                fail_count += 1
                continue

            run.refresh_from_db()
            if run.is_complete:
                printer.print_step_done(cid)
                success_count += 1
            elif run.has_failed:
                printer.error(f"Failed: {run.error_message}")
                fail_count += 1
            else:
                printer.warn("Status unclear")

        # ---- Summary ----
        printer.print_summary(len(target_runs), success_count, skip_count, fail_count)
