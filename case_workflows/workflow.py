"""
Workflow ABC — defines the contract for all case workflow templates,
and owns the execution logic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, List, Optional

from asgiref.sync import sync_to_async
from django.conf import settings

from case_workflows.output import WorkflowPrinter
from case_workflows.storage_utils import (
    download_workflow_outputs,
    upload_workflow_outputs,
)
from case_workflows.unicode_repair_middleware import UnicodeEscapeRepairMiddleware

if TYPE_CHECKING:
    from case_workflows.models import CaseWorkflowRun

logger = logging.getLogger(__name__)

_WORK_DIR_ENV = "JAWAFDEHI_ALLOWED_WORK_DIR"


def _patch_gemini_null_properties() -> None:
    """
    Patch langchain_google_genai to handle tool schemas that Gemini rejects.

    Root cause: Gemini requires ``type`` to be explicitly ``"object"`` whenever
    ``properties`` is present.  Valid JSON Schema omits ``type`` when it is
    implied (e.g. Pydantic v2 model schemas, dereferenced ``$ref`` objects), but
    Gemini's strict validation rejects that.

    Two patches are applied:

    1. **``_format_json_schema_to_gapic``** — this is the function inside
       ``langchain_google_genai._function_utils`` that converts raw JSON Schema
       into the intermediate ``formatted_schema`` dict that
       ``_dict_to_genai_schema`` then reads.  After conversion, if the result
       has ``properties`` but no ``type`` (and no ``anyOf``), we add
       ``"type": "object"``.  Because the function recurses via LOAD_GLOBAL,
       this fix also covers every nested schema produced by its own recursion
       (including ``$ref``-expanded sub-schemas).

    2. **``_dict_to_genai_schema``** — kept for the original null-property
       fix: property slots whose schema converts to ``None`` crash pydantic;
       return an empty ``Schema()`` instead.

    Version guard: upstream langchain-google-genai >= 4.2.1 is expected to
    ship a proper fix for the null-property issue.  When that version (or
    later) is detected, this patch no-ops so we don't fight the library.
    """
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

        try:
            _lgn_ver_str = _pkg_version("langchain-google-genai")
            # Parse major.minor.patch without requiring the `packaging` library.
            _lgn_ver = tuple(int(x) for x in _lgn_ver_str.split(".")[:3] if x.isdigit())
            if _lgn_ver >= (4, 2, 1):
                # Upstream ships a fix in this version; skip the monkey-patch.
                return
        except (PackageNotFoundError, ValueError):
            pass  # Unknown version — apply the patch defensively.

        import langchain_google_genai._function_utils as _fu
        from google.genai import types as _genai_types

        # --- patch 1: _format_json_schema_to_gapic ---
        _orig_format = _fu._format_json_schema_to_gapic

        def _patched_format(schema: Any) -> Any:
            result = _orig_format(schema)
            if not isinstance(result, dict):
                return result

            # Pydantic v2 sometimes emits schemas with BOTH "properties" AND
            # "anyOf" where anyOf items are pure required-constraint dicts:
            #   anyOf: [{"required": ["x"]}, {"required": ["y"]}]
            # These carry no type information and Gemini rejects them with:
            #   "parameters.any_of[N].required: only allowed for OBJECT type"
            # Additionally, when anyOf is present alongside properties,
            # _dict_to_genai_schema intentionally drops "type" (per its comment
            # "when any_of is used, it must be the only field set"), so the
            # resulting Schema has properties but no type — also rejected by Gemini.
            #
            # Fix: remove anyOf items that are ONLY a required-constraint (no
            # type, no properties, no anyOf of their own).  If all items are
            # stripped, delete the anyOf key entirely so that _dict_to_genai_schema
            # will correctly set type=OBJECT for the parent schema.
            if "anyOf" in result:
                kept = [
                    item
                    for item in result["anyOf"]
                    if not (
                        isinstance(item, dict)
                        and "required" in item
                        and "type" not in item
                        and "properties" not in item
                        and "anyOf" not in item
                    )
                ]
                if kept:
                    result["anyOf"] = kept
                else:
                    del result["anyOf"]

            # After the cleanup above, if properties are present but type is
            # absent (and no anyOf remains), add the explicit "object" type that
            # Gemini requires.
            if (
                "properties" in result
                and "type" not in result
                and "anyOf" not in result
            ):
                result["type"] = "object"

            return result

        _fu._format_json_schema_to_gapic = _patched_format

        # --- patch 2: _dict_to_genai_schema (null-property fix) ---
        _orig_dict = _fu._dict_to_genai_schema

        def _patched_dict(
            schema, is_property: bool = False, is_any_of_item: bool = False
        ):
            result = _orig_dict(
                schema, is_property=is_property, is_any_of_item=is_any_of_item
            )
            if result is None and is_property:
                return _genai_types.Schema()
            return result

        _fu._dict_to_genai_schema = _patched_dict

    except ImportError:
        pass  # langchain_google_genai not installed; skip


_patch_gemini_null_properties()


def _patch_unicode_file_editing() -> None:
    """
    Patch deepagents ``perform_string_replacement`` to handle Unicode normalisation.

    Root cause: The agent (LLM) generates Devanagari text in NFD normalisation
    while files on disk are stored in NFC (or vice-versa).  The two forms look
    identical but differ at the byte level, so the bare ``content.count(old_string)``
    inside ``perform_string_replacement`` reports 0 occurrences and the edit fails
    with "String not found in file".

    Fix: normalise both the file content AND the search string to NFC before
    comparing and replacing.  NFC is a canonical normalisation so the resulting
    file content is semantically identical.
    """
    try:
        import unicodedata

        import deepagents.backends.utils as _dbu

        _orig_replace = _dbu.perform_string_replacement

        def _nfc_replace(
            content: str,
            old_string: str,
            new_string: str,
            replace_all: bool = False,
        ):
            # Normalise all three strings to NFC so that visually identical
            # Devanagari (and other Unicode) text matches regardless of how it
            # was generated or stored.
            content_nfc = unicodedata.normalize("NFC", content)
            old_nfc = unicodedata.normalize("NFC", old_string)
            new_nfc = unicodedata.normalize("NFC", new_string)
            return _orig_replace(content_nfc, old_nfc, new_nfc, replace_all)

        _dbu.perform_string_replacement = _nfc_replace

        # The filesystem backend imports perform_string_replacement at call-time
        # via module reference, but patch it there too just in case it was
        # imported directly.
        try:
            import deepagents.backends.filesystem as _dbf

            if hasattr(_dbf, "perform_string_replacement"):
                _dbf.perform_string_replacement = _nfc_replace
        except ImportError:
            pass

    except ImportError:
        pass  # deepagents not installed; skip


_patch_unicode_file_editing()


# ---------------------------------------------------------------------------
# WorkflowStep
# ---------------------------------------------------------------------------


@dataclass
class WorkflowStep:
    """A single step within a workflow template."""

    name: str
    prompt_fn: Callable[[Path], str]
    skills: List[str] = field(default_factory=list)
    tools: List[Any] = field(default_factory=list)
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    mcp_servers_fn: Optional[Callable[[], dict[str, dict]]] = None
    mcp_tool_filter: Optional[List[str]] = None
    subagents: List[dict] = field(default_factory=list)
    system_prompt: Optional[str] = None
    required_outputs: dict[str, int] = field(default_factory=dict)
    retries: int = 0

    def resolved_mcp_servers(self) -> dict[str, dict]:
        """Return mcp_servers, merging in mcp_servers_fn() if provided.

        ``mcp_servers_fn`` is called lazily at execution time so that
        runtime-dependent values (e.g. API tokens read from env vars) are
        not evaluated when the step list is constructed or inspected.
        """
        if self.mcp_servers_fn is not None:
            return {**self.mcp_servers, **self.mcp_servers_fn()}
        return self.mcp_servers


# ---------------------------------------------------------------------------
# Workflow ABC
# ---------------------------------------------------------------------------


class Workflow(ABC):
    """
    Abstract base class for all case workflow templates.

    Subclasses live under ``case_workflows/workflows/<template_id>/workflow.py``
    and are auto-discovered by the registry on Django startup.

    Each instance doubles as both the template definition **and** the executor:

    .. code-block:: python

        workflow = get_workflow("ciaa_caseworker")
        work_dir = workflow.setup_work_dir(run)
        workflow.execute(run, model="openai:gpt-4o")
    """

    # ------------------------------------------------------------------
    # Template contract (must implement in subclasses)
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def workflow_id(self) -> str:
        """Unique identifier matching the template directory name."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable name shown in CLI output and admin."""
        ...

    @property
    @abstractmethod
    def steps(self) -> List[WorkflowStep]:
        """Ordered list of workflow steps."""
        ...

    @abstractmethod
    def get_eligible_cases(self) -> List[str]:
        """
        Return a list of Jawafdehi ``Case.case_id`` strings eligible for
        this workflow (e.g. ``["case-abc123", "case-def456"]``).
        """
        ...

    # ------------------------------------------------------------------
    # Optional overrides
    # ------------------------------------------------------------------

    def get_template_dir(self) -> Path:
        """Directory containing this template's files (default: module dir)."""
        return Path(__file__).resolve().parent

    def get_instructions_dir(self) -> Path:
        """Directory containing instruction files for the agent."""
        return self.get_template_dir() / "instructions"

    def on_work_dir_created(self, case_dir: Path) -> None:
        """
        Called after the base work directory is created.

        Override to add template-specific directory structure or files.
        Default is a no-op.
        """

    def on_initialize(self) -> None:
        """
        Called once before execution begins.

        Override to perform any one-time setup. Default is a no-op.
        """

    # ------------------------------------------------------------------
    # Work directory
    # ------------------------------------------------------------------

    def get_work_dir(self, run: CaseWorkflowRun) -> Path:
        """
        Return the work directory path for a given run.

        Structure: ``<CASE_WORKFLOWS_WORK_DIR>/<workflow_id>/<case_id>/``
        """
        base = getattr(
            settings,
            "CASE_WORKFLOWS_WORK_DIR",
            Path(settings.BASE_DIR) / "workflow-runs",
        )
        return Path(base) / self.workflow_id / run.case_id

    def setup_work_dir(
        self, run: CaseWorkflowRun, *, preserve_existing: bool = False
    ) -> Path:
        """
        Create the work directory for a run and populate it.

                - If the directory already exists, either recreates it or preserves it
                    when ``preserve_existing=True``
        - Downloads any previously uploaded files tracked in ``run.case_data``
        - Calls ``on_work_dir_created(case_dir)`` for template-specific setup
        - Initialises ``run.case_data`` if empty

        Persists ``run.work_dir`` and returns the directory path.
        """
        import shutil

        case_dir = self.get_work_dir(run)
        existed_before = case_dir.is_dir()

        if existed_before and not preserve_existing:
            logger.warning(
                "Work directory already exists — deleting and recreating: %s", case_dir
            )
            shutil.rmtree(case_dir)
        elif existed_before and preserve_existing:
            logger.info("Work directory exists — preserving for resume: %s", case_dir)

        case_dir.mkdir(parents=True, exist_ok=True)

        # Initialise state in case_data if this is a fresh run
        if not run.case_data:
            run.case_data = {"is_complete": False, "steps": {}, "files": {}}

        # Download existing tracked files if resuming a previous run
        tracked_files = run.case_data.get("files", {})
        if tracked_files:
            download_workflow_outputs(case_dir, tracked_files)

        # Template-specific bootstrap is only needed for fresh directories.
        if not (preserve_existing and existed_before):
            self.on_work_dir_created(case_dir)

        run.work_dir = str(case_dir)
        run.save(update_fields=["work_dir", "case_data", "updated_at"])

        logger.info("Work directory created: %s", case_dir)
        return case_dir

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute(
        self,
        run: CaseWorkflowRun,
        *,
        model: str = "openai:gpt-4o",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        verbose: bool = False,
        recursion_limit: int = 200,
        printer: Optional[WorkflowPrinter] = None,
        resume_from_step: Optional[str] = None,
    ) -> None:
        """
        Run all pending workflow steps in order using a deepagents agent.

        Each step builds its own agent with the step's tools, MCP servers,
        system prompt, and subagents.  The agent receives filesystem access
        to ``case_dir`` via ``FilesystemBackend``.

        Step completion is stored in ``run.case_data["steps"]`` and
        persisted to the database after every step so that interrupted
        runs can be resumed from the correct point.
        """
        asyncio.run(
            self._execute_async(
                run,
                model=model,
                api_key=api_key,
                base_url=base_url,
                verbose=verbose,
                recursion_limit=recursion_limit,
                printer=printer,
                resume_from_step=resume_from_step,
            )
        )

    async def _execute_async(
        self,
        run: CaseWorkflowRun,
        *,
        model: str,
        api_key: Optional[str],
        base_url: Optional[str],
        verbose: bool = False,
        recursion_limit: int = 200,
        printer: Optional[WorkflowPrinter] = None,
        resume_from_step: Optional[str] = None,
    ) -> None:

        from deepagents import create_deep_agent
        from deepagents.backends import FilesystemBackend
        from langchain.chat_models import init_chat_model
        from langchain_core.callbacks import BaseCallbackHandler
        from langchain_mcp_adapters.client import MultiServerMCPClient

        class _UsageCallbackHandler(BaseCallbackHandler):
            """Accumulate LLM token usage across all steps."""

            def __init__(self) -> None:
                super().__init__()
                self.input_tokens: int = 0
                self.output_tokens: int = 0

            @property
            def total_tokens(self) -> int:
                return self.input_tokens + self.output_tokens

            def on_llm_end(self, response: Any, **kwargs: Any) -> None:  # type: ignore[override]
                for row in response.generations:
                    for gen in row:
                        meta = getattr(
                            getattr(gen, "message", None), "usage_metadata", None
                        )
                        if meta:
                            self.input_tokens += meta.get("input_tokens", 0)
                            self.output_tokens += meta.get("output_tokens", 0)

        case_dir = self.get_work_dir(run)
        if not case_dir.is_dir():
            raise RuntimeError(
                f"Work directory does not exist — call setup_work_dir() first: {case_dir}"
            )

        # Initialise or restore state
        state: dict = run.case_data or {}
        if "steps" not in state:
            state.update({"is_complete": False, "steps": {}, "files": {}})

        if state.get("is_complete"):
            await sync_to_async(run.mark_complete)(case_data=state)
            return

        resume_step_index = 0
        if resume_from_step is not None:
            step_names = [step.name for step in self.steps]
            if resume_from_step not in step_names:
                raise RuntimeError(
                    f"Unknown resume step '{resume_from_step}'. Available steps: {step_names}"
                )
            resume_step_index = step_names.index(resume_from_step)
            if printer:
                printer.warn(f"Resuming run from step: {resume_from_step}")
            else:
                logger.info("Resuming run from step: %s", resume_from_step)

        await sync_to_async(run.mark_started)()
        self.on_initialize()

        # Build the chat model (supports any OpenAI-compatible provider)
        model_kwargs: dict[str, Any] = {}
        if api_key:
            model_kwargs["api_key"] = api_key
        if base_url:
            model_kwargs["base_url"] = base_url
        chat_model = init_chat_model(model, **model_kwargs)

        # Build Langfuse callback if credentials are present
        langfuse_callbacks: list[Any] = []
        if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get(
            "LANGFUSE_SECRET_KEY"
        ):
            import uuid

            from langfuse.langchain import CallbackHandler

            trace_id = uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"jawafdehi/{self.workflow_id}/{run.case_id}",
            ).hex  # 32 lowercase hex chars
            langfuse_callbacks = [
                CallbackHandler(
                    trace_context={"trace_id": trace_id},
                )
            ]
            if printer:
                printer.print_tracing_info("Langfuse")
            else:
                logger.info("Langfuse tracing enabled")

        # Pre-connect all unique MCP servers once across all steps so that
        # uvx only resolves/installs each server a single time.
        # resolved_mcp_servers() is called here (at execution time) so that
        # any mcp_servers_fn callables are evaluated lazily — not when the
        # step list is constructed or inspected.
        all_mcp_servers: dict[str, dict] = {}
        for step in self.steps:
            all_mcp_servers.update(step.resolved_mcp_servers())

        usage_tracker = _UsageCallbackHandler()

        mcp_tools_by_server: dict[str, list[Any]] = {}
        if all_mcp_servers:
            for server_name, server_config in all_mcp_servers.items():
                client = MultiServerMCPClient({server_name: server_config})
                tools = await client.get_tools()
                for t in tools:
                    t.handle_tool_error = True
                mcp_tools_by_server[server_name] = tools
            if printer:
                printer.print_mcp_info(list(mcp_tools_by_server))
            else:
                logger.info("MCP servers connected: %s", list(mcp_tools_by_server))

        total_steps = len(self.steps)
        try:
            for idx, step in enumerate(self.steps, start=1):
                if resume_from_step is not None and idx - 1 < resume_step_index:
                    if printer:
                        printer.print_step_skipped(step.name)
                    else:
                        logger.info("Skipping step before resume target: %s", step.name)
                    continue

                step_state = state["steps"].get(step.name, {})
                if step_state.get("status") == "complete":
                    if printer:
                        printer.print_step_skipped(step.name)
                    else:
                        logger.info("Skipping completed step: %s", step.name)
                    continue

                if printer:
                    printer.print_step_header(step.name, idx, total_steps)
                else:
                    logger.info("Running step: %s", step.name)

                max_attempts = max(1, 1 + step.retries)
                prior_attempts = list(step_state.get("attempts", []))
                attempts = prior_attempts

                for attempt in range(1, max_attempts + 1):
                    if step.name == "draft-case":
                        self._validate_draft_inputs(case_dir)

                    # Gather pre-connected MCP tools for this step's servers
                    mcp_tools: list[Any] = []
                    for server_name in step.resolved_mcp_servers():
                        mcp_tools.extend(mcp_tools_by_server.get(server_name, []))
                    if step.mcp_tool_filter is not None:
                        allowed = set(step.mcp_tool_filter)
                        mcp_tools = [t for t in mcp_tools if t.name in allowed]

                    all_tools = mcp_tools + step.tools

                    # Pass MEMORY.md as agent memory so it is injected into the
                    # system prompt at startup and the agent can write back to it
                    # with `edit_file` to record learnings across steps.
                    memory_file = case_dir / "MEMORY.md"
                    memory_paths = [str(memory_file)] if memory_file.is_file() else None

                    agent = create_deep_agent(
                        model=chat_model,
                        tools=all_tools,
                        system_prompt=step.system_prompt,
                        subagents=step.subagents or None,
                        backend=FilesystemBackend(
                            root_dir=str(case_dir), virtual_mode=False
                        ),
                        memory=memory_paths,
                        middleware=[UnicodeEscapeRepairMiddleware()],
                    )

                    files_before = set(self._list_relative_files(case_dir))
                    prompt_text = self._build_step_prompt(
                        step,
                        case_dir,
                        attempt=attempt,
                        max_attempts=max_attempts,
                    )

                    invocation = {
                        "messages": [
                            {
                                "role": "user",
                                "content": prompt_text,
                            }
                        ]
                    }
                    run_config = {
                        "callbacks": langfuse_callbacks + [usage_tracker],
                        "run_name": step.name,
                        "recursion_limit": recursion_limit,
                    }

                    # Record attempt start time and persist before running
                    attempt_started_dt = datetime.now(timezone.utc)
                    attempt_started_at = attempt_started_dt.isoformat()
                    attempt_state: dict[str, Any] = {
                        "attempt": attempt,
                        "started_at": attempt_started_at,
                    }

                    state["steps"][step.name] = {
                        "status": "in_progress",
                        "started_at": (
                            attempts[0]["started_at"]
                            if attempts
                            else attempt_started_at
                        ),
                        "current_attempt": attempt,
                        "max_attempts": max_attempts,
                        "attempts": attempts + [attempt_state],
                    }
                    run.case_data = state
                    await sync_to_async(run.save)(
                        update_fields=["case_data", "updated_at"]
                    )

                    try:
                        previous_allowed_work_dir = os.environ.get(_WORK_DIR_ENV)
                        os.environ[_WORK_DIR_ENV] = str(case_dir.resolve())
                        created_files: list[str] = []
                        if verbose:
                            async for event in agent.astream_events(
                                invocation, config=run_config, version="v2"
                            ):
                                if printer:
                                    printer.handle_agent_event(event)
                        else:
                            await agent.ainvoke(invocation, config=run_config)

                        created_files = self._detect_created_files(
                            files_before,
                            set(self._list_relative_files(case_dir)),
                        )
                        self._validate_step_outputs(case_dir, step)

                        attempt_completed_dt = datetime.now(timezone.utc)
                        elapsed_seconds = (
                            attempt_completed_dt - attempt_started_dt
                        ).total_seconds()
                        # Mutate attempt_state in-place: the pre-run snapshot at line
                        # ~618 captured the same dict object before these fields existed,
                        # so updating here enriches that persisted record too.  Then we
                        # append to `attempts` (the local accumulator) so the final
                        # state[steps][...]["attempts"] list grows by exactly one entry
                        # per attempt — no duplicate risk.
                        attempt_state.update(
                            {
                                "status": "complete",
                                "completed_at": attempt_completed_dt.isoformat(),
                                "duration_seconds": round(elapsed_seconds, 3),
                                "created_files": created_files,
                                "created_files_count": len(created_files),
                            }
                        )
                        attempts.append(attempt_state)

                        state["steps"][step.name] = {
                            "status": "complete",
                            "started_at": attempts[0]["started_at"],
                            "completed_at": attempt_completed_dt.isoformat(),
                            "duration_seconds": round(elapsed_seconds, 3),
                            "current_attempt": attempt,
                            "max_attempts": max_attempts,
                            "attempts": attempts,
                        }

                        previous_files = state.get("files", {})
                        uploaded = self._upload_outputs(
                            case_dir, run.case_id, previous_files
                        )
                        state.setdefault("files", {}).update(uploaded)

                        run.case_data = state
                        await sync_to_async(run.save)(
                            update_fields=["case_data", "updated_at"]
                        )
                        if printer:
                            printer.print_step_done(step.name, elapsed_seconds)
                        else:
                            logger.info(
                                "Step completed: %s (%.2fs)",
                                step.name,
                                elapsed_seconds,
                            )
                        break

                    except Exception as step_exc:
                        created_files = self._detect_created_files(
                            files_before,
                            set(self._list_relative_files(case_dir)),
                        )
                        attempt_completed_dt = datetime.now(timezone.utc)
                        elapsed_seconds = (
                            attempt_completed_dt - attempt_started_dt
                        ).total_seconds()
                        attempt_state.update(
                            {
                                "status": "failed",
                                "completed_at": attempt_completed_dt.isoformat(),
                                "duration_seconds": round(elapsed_seconds, 3),
                                "error": str(step_exc),
                                "created_files": created_files,
                                "created_files_count": len(created_files),
                            }
                        )
                        attempts.append(attempt_state)

                        state["steps"][step.name] = {
                            "status": (
                                "failed" if attempt == max_attempts else "in_progress"
                            ),
                            "started_at": attempts[0]["started_at"],
                            "completed_at": attempt_completed_dt.isoformat(),
                            "duration_seconds": round(elapsed_seconds, 3),
                            "current_attempt": attempt,
                            "max_attempts": max_attempts,
                            "attempts": attempts,
                        }
                        run.case_data = state
                        await sync_to_async(run.save)(
                            update_fields=["case_data", "updated_at"]
                        )

                        if attempt < max_attempts:
                            retry_msg = f"Step '{step.name}' attempt {attempt}/{max_attempts} failed: {step_exc}. Retrying..."
                            if printer:
                                printer.warn(retry_msg)
                            else:
                                logger.warning(retry_msg)
                            continue
                        raise
                    finally:
                        if previous_allowed_work_dir is None:
                            os.environ.pop(_WORK_DIR_ENV, None)
                        else:
                            os.environ[_WORK_DIR_ENV] = previous_allowed_work_dir

            # All steps done
            state["is_complete"] = True
            await sync_to_async(run.mark_complete)(case_data=state)
            logger.info("Workflow completed for %s", run.case_id)

            if printer:
                printer.print_usage_summary(
                    usage_tracker.input_tokens, usage_tracker.output_tokens
                )
            else:
                logger.info(
                    "Token usage — input: %d, output: %d, total: %d",
                    usage_tracker.input_tokens,
                    usage_tracker.output_tokens,
                    usage_tracker.total_tokens,
                )

        except Exception as exc:
            await sync_to_async(run.mark_failed)(error_message=str(exc))
            logger.exception("Workflow execution error for %s", run.case_id)
            raise

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _list_relative_files(case_dir: Path) -> list[str]:
        """List all files in a case directory as stable relative paths."""
        return sorted(
            str(path.relative_to(case_dir))
            for path in case_dir.rglob("*")
            if path.is_file()
        )

    @staticmethod
    def _detect_created_files(before: set[str], after: set[str]) -> list[str]:
        """Return files newly created during the step attempt."""
        return sorted(after - before)

    @staticmethod
    def _build_step_prompt(
        step: WorkflowStep,
        case_dir: Path,
        *,
        attempt: int,
        max_attempts: int,
    ) -> str:
        """Build user prompt for the current step attempt."""
        prompt = step.prompt_fn(case_dir)
        if attempt <= 1:
            return prompt

        fallback: str | None = None

        if step.name == "draft-case":
            fallback = f"""

Retry context: attempt {attempt}/{max_attempts}.

Previous attempt failed because required output `draft.md` was missing.
You MUST create `{case_dir}/draft.md` immediately using write_file before any long analysis.

Use fallback mode:
1. Start from core sources only: case_details-*.md, charge-sheet-*.md, ciaa-press-release-*.md.
2. Write a complete template-compliant draft with minimum required sections first.
3. Expand and refine with news sources only after the initial draft file exists.
4. Before finishing, verify `{case_dir}/draft.md` exists and is not empty.
"""
        elif step.name == "review-draft":
            fallback = f"""

Retry context: attempt {attempt}/{max_attempts}.

Previous attempt failed because required output `draft-review.md` was missing.
You MUST create `{case_dir}/draft-review.md` immediately using write_file before any long analysis.

Use fallback mode:
1. Write a minimally complete review skeleton first, including severity headings and the final outcome line.
2. Start with the highest-risk checks: identity, allegations, amounts, timeline, procedural status, sources, template compliance, and common pitfalls.
3. Refine the review iteratively as you inspect each source markdown file and note confirming facts, conflicts, or missing support.
4. Before finishing, verify `{case_dir}/draft-review.md` exists and is not empty.
"""

        if fallback is None:
            return prompt

        return prompt + fallback

    @staticmethod
    def _read_markdown_resilient(path: Path) -> str:
        """Read text as UTF-8 and recover safely when bytes are invalid."""
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            logger.warning(
                "Invalid UTF-8 in %s at byte %s; decoding with replacement",
                path,
                exc.start,
            )
            return path.read_text(encoding="utf-8", errors="replace")

    @classmethod
    def _log_invalid_utf8_sources(cls, case_dir: Path) -> None:
        """Log source markdown files that contain invalid UTF-8 bytes."""
        markdown_dir = case_dir / "sources" / "markdown"
        if not markdown_dir.is_dir():
            return

        for src in sorted(markdown_dir.glob("*.md")):
            try:
                src.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                logger.warning(
                    "Source markdown contains invalid UTF-8: %s (byte %s)",
                    src,
                    exc.start,
                )

    @staticmethod
    def _validate_draft_inputs(case_dir: Path) -> None:
        """Validate source quality and article-volume policy before drafting."""
        Workflow._log_invalid_utf8_sources(case_dir)
        markdown_dir = case_dir / "sources" / "markdown"
        news_files = sorted(markdown_dir.glob("news-*.md"))

        if len(news_files) > 10:
            summary_path = case_dir / "logs" / "news-search-summary.md"
            summary_text = ""
            if summary_path.is_file():
                summary_text = Workflow._read_markdown_resilient(summary_path)
            has_override = bool(
                re.search(
                    r"\b(override|exception|reason)\b",
                    summary_text,
                    flags=re.IGNORECASE,
                )
            )
            logger.warning(
                "draft-case input policy warning: found %s news markdown files (max 10); %sdocumented override reason in logs/news-search-summary.md. Drafting will continue.",
                len(news_files),
                "" if has_override else "no ",
            )

        escaped_pattern = re.compile(r"\bu[0-9a-fA-F]{4}\b")
        escaped_candidates: list[str] = []
        for src in markdown_dir.glob("*.md"):
            text = Workflow._read_markdown_resilient(src)
            if len(escaped_pattern.findall(text)) >= 20:
                escaped_candidates.append(src.name)

        if escaped_candidates:
            raise RuntimeError(
                "draft-case input quality violation: escaped-unicode-heavy markdown detected in "
                + ", ".join(sorted(escaped_candidates))
                + ". Normalize or regenerate these files before drafting."
            )

    @staticmethod
    def _upload_outputs(
        case_dir: Path, case_id: str, previous_files: dict[str, dict] | None = None
    ) -> dict[str, dict]:
        """Upload all non-excluded files in *case_dir* to ``default_storage``."""
        try:
            return upload_workflow_outputs(case_dir, case_id, previous_files)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to upload workflow outputs for %s: %s", case_id, exc)
            return {}

    @staticmethod
    def _validate_step_outputs(case_dir: Path, step: WorkflowStep) -> None:
        """Ensure required step outputs exist and are non-trivial."""
        missing_or_invalid: list[str] = []

        for rel_path, min_bytes in step.required_outputs.items():
            output_path = case_dir / rel_path
            if not output_path.is_file():
                missing_or_invalid.append(f"missing {rel_path}")
                continue

            size = output_path.stat().st_size
            if size < min_bytes:
                missing_or_invalid.append(
                    f"{rel_path} too small ({size} bytes < {min_bytes} bytes)"
                )

        if missing_or_invalid:
            details = "; ".join(missing_or_invalid)
            raise RuntimeError(
                f"Step '{step.name}' did not produce required outputs: {details}"
            )
