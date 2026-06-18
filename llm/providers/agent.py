"""Agentic, sandboxed claude-CLI provider with an iterate-until-complete loop.

Unlike ``claude_cli`` (locked to a single read-only ``--permission-mode plan`` turn
with no tools), this provider lets the claude CLI RUN WILD: multi-turn, reading the
case materials we PRE-STAGE for it as markdown files (the case data, the converted
source documents, and the authoritative NGM court record). It does NOT fetch its own
sources — an earlier "fetch via MCP" design under-read the sources and missed
integrity checks, so the pipeline now hands it complete, pre-converted materials.

After each run a cheaper model judges whether the work is complete; if not, we re-run
with the gaps, up to a hard iteration cap. The final assistant text (the requested
JSON) is returned exactly like every other provider, so the rest of the pipeline is
unchanged. Sandbox: cwd + ``--add-dir`` confine file reads to the ephemeral staging
dir; no shell, no writes, no network. A minimal MCP allowlist (``convert_date`` now,
extensible) is kept for date math and future tools. Auth is the seeded subscription
OAuth (no API keys).
"""

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

from django.conf import settings

from llm.providers.base import strip_code_fence
from llm.providers.cli import _CliProvider

logger = logging.getLogger(__name__)

# Marker (from judge._rule_context_block) that separates the case data from the
# source-document block, used to split the staged materials into readable files.
_SOURCES_MARKER = "SOURCE DOCUMENT EXCERPTS"


def _as_list(value):
    """Split a space/comma separated tool list into argv tokens (empty -> [])."""
    if not value:
        return []
    return [t for t in value.replace(",", " ").split() if t]


def _split_content(content):
    """Split judge content into (materials, instruction).

    The judge hands either a 2-block list ``[{context}, {rules instruction}]`` (prompt
    cache on) or a single concatenated string. We stage the bulky context as files and
    keep the (small) rules instruction as the stdin task. For a plain string we have no
    clean split, so everything is staged and the task points at the files.
    """
    if isinstance(content, str):
        return content, ""
    blocks = [b.get("text", "") for b in content if isinstance(b, dict)]
    if not blocks:
        return "", ""
    return blocks[0], "\n\n".join(b for b in blocks[1:] if b)


def _stage_materials(staging_dir, materials):
    """Write the case materials into the staging dir; return the filenames written."""
    d = Path(staging_dir)
    idx = materials.find(_SOURCES_MARKER)
    if idx != -1:
        (d / "case.md").write_text(materials[:idx].rstrip() + "\n", encoding="utf-8")
        (d / "sources.md").write_text(materials[idx:], encoding="utf-8")
        return ["case.md", "sources.md"]
    (d / "materials.md").write_text(materials, encoding="utf-8")
    return ["materials.md"]


def _build_task(files, instruction):
    """The stdin task: read the staged files, then grade per the instruction."""
    preamble = (
        f"Your working directory contains the case materials as markdown files: "
        f"{', '.join(files)}. READ them in full before grading — the case data, the "
        "converted source documents, and the official NGM court record are there. "
        "Ground every judgement in those files; do not invent facts. You may call the "
        "convert_date tool for Bikram-Sambat<->Gregorian date conversions. Reply with "
        "ONE JSON object exactly as instructed and nothing else."
    )
    if instruction:
        return f"{preamble}\n\n{instruction}"
    return (
        f"{preamble}\n\nFollow the grading instructions contained at the end of the "
        "materials files."
    )


class ClaudeAgentProvider(_CliProvider):
    """Run the claude CLI agentically over pre-staged files, looping until satisfied."""

    name = "claude_agent"

    def invoke_text(self, system, content, max_tokens, model_id, tier, usage=None):
        """Stage materials -> run-wild over files -> judge -> rerun; return JSON text."""
        materials, instruction = _split_content(content)
        staging = tempfile.mkdtemp(prefix="review-agent-")
        try:
            files = _stage_materials(staging, materials)
            base_task = _build_task(files, instruction)
            argv = self._build_argv(system, model_id, staging)
            env = self._build_env()
            timeout = int(getattr(settings, "CLAUDE_AGENT_TIMEOUT", 900))
            max_iters = max(1, int(getattr(settings, "CLAUDE_AGENT_MAX_ITERS", 3)))
            # What the completeness judge grades against (the rules ask, or the
            # whole materials when content arrived as one undelimited string).
            judge_task = instruction or materials

            answer = ""
            missing = []
            trace = []  # numbered list of iterations, for observability
            for i in range(1, max_iters + 1):
                stdin_text = (
                    base_task if i == 1 else self._retry_prompt(base_task, missing)
                )
                out = self._run(argv, stdin_text, env, timeout=timeout, cwd=staging)
                answer = self._parse_result(out, model_id, tier, usage)
                complete, missing = self._judge_complete(judge_task, answer, usage)
                trace.append(
                    f"  {i}. read+grade -> complete={complete}"
                    + ("" if complete else f"; missing={missing}")
                )
                if complete:
                    break

            logger.info(
                "claude_agent finished in %d/%d iteration(s) [tier=%s]:\n%s",
                len(trace),
                max_iters,
                tier,
                "\n".join(trace),
            )
            return answer
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    def model_for_tier(self, tier):
        """Resolve tier -> claude model id/alias (premium for gates, cheap for bulk)."""
        fallback = getattr(settings, "CLAUDE_AGENT_MODEL_ID", "")
        if tier == "premium":
            return getattr(settings, "CLAUDE_AGENT_MODEL_PREMIUM", "") or fallback
        return getattr(settings, "CLAUDE_AGENT_MODEL_CHEAP", "") or fallback

    # -- internals ---------------------------------------------------------

    def _build_argv(self, system, model_id, staging_dir):
        """Headless but AGENTIC claude, sandboxed to read only the staging dir."""
        argv = [
            getattr(settings, "CLAUDE_AGENT_BIN", "claude"),
            "-p",
            "--output-format",
            "json",
            "--system-prompt",
            system,
            "--max-turns",
            str(int(getattr(settings, "CLAUDE_AGENT_MAX_TURNS", 40))),
            # Confine filesystem access to the ephemeral staging dir (cwd is set to
            # it by _run); the agent can Read the staged materials, nothing else.
            "--add-dir",
            staging_dir,
        ]
        if model_id:
            argv.extend(["--model", model_id])

        mcp_config = getattr(settings, "CLAUDE_AGENT_MCP_CONFIG", "")
        if mcp_config:
            # --strict-mcp-config ignores any ambient ~/.claude MCP servers, so the
            # agent reaches ONLY the predefined toolset (predictable + isolated).
            argv.extend(["--mcp-config", mcp_config, "--strict-mcp-config"])

        # Allowlist: file-read tools (scoped by --add-dir) + a minimal MCP set.
        # Everything else (shell, writes, web) is denied.
        allowed = _as_list(getattr(settings, "CLAUDE_AGENT_ALLOWED_TOOLS", ""))
        if allowed:
            argv.extend(["--allowedTools", *allowed])
        disallowed = _as_list(getattr(settings, "CLAUDE_AGENT_DISALLOWED_TOOLS", ""))
        if disallowed:
            argv.extend(["--disallowedTools", *disallowed])
        return argv

    def _build_env(self):
        """Subscription OAuth via CLAUDE_AGENT_HOME; never leak an API key."""
        env = dict(os.environ)
        home = getattr(settings, "CLAUDE_AGENT_HOME", "")
        if home:
            env["HOME"] = home
        env.pop("ANTHROPIC_API_KEY", None)
        return env

    def _parse_result(self, out, model_id, tier, usage):
        """Extract data['result'] from the claude -p JSON envelope + record usage."""
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"claude_agent: failed to parse JSON envelope: {e}\nOutput: {out[-500:]}"
            )
        if not isinstance(data, dict) or data.get("is_error") or "result" not in data:
            payload = (
                data.get("result", "unknown error")
                if isinstance(data, dict)
                else out[-500:]
            )
            raise RuntimeError(f"claude_agent error: {payload}")

        if usage is not None:
            u = data.get("usage", {}) or {}
            model_usage = data.get("modelUsage", {}) or {}
            reported_model = (
                next(iter(model_usage)) if model_usage else (model_id or "claude")
            )
            input_tokens = (
                u.get("input_tokens", 0)
                + u.get("cache_creation_input_tokens", 0)
                + u.get("cache_read_input_tokens", 0)
            )
            usage.add(
                input_tokens,
                u.get("output_tokens", 0),
                provider="claude_agent",
                tier=tier,
                model=reported_model,
                cost_usd=data.get("total_cost_usd", 0.0),
            )
        return strip_code_fence(data["result"])

    def _retry_prompt(self, base_task, missing):
        """Re-prompt feeding back the judge's gaps so the next run completes them."""
        gaps = "; ".join(missing) if missing else "the answer was incomplete"
        return (
            f"{base_task}\n\n---\nYour previous answer did NOT fully satisfy the task. "
            f"Gaps found: {gaps}.\nRe-read the materials, redo the work, and reply with "
            "the COMPLETE, corrected JSON in the exact shape requested — nothing else."
        )

    def _judge_complete(self, task, answer, usage):
        """Return (complete, missing). Structural valid-JSON check + cheap-model grade.

        The structural check is a free fast-path. The cheap-model grade is generic
        (it never needs to know rule keys) so the same loop serves review AND
        enrichment. Judge failures are non-fatal: we fall back to the structural
        verdict so a flaky judge can't burn the iteration budget.
        """
        from llm.invoke import salvage_json

        try:
            salvage_json(answer)
        except (
            Exception
        ):  # noqa: BLE001 - unparseable answer is definitionally incomplete
            return False, ["response is not valid JSON in the requested shape"]

        judge_name = getattr(settings, "CLAUDE_AGENT_JUDGE_PROVIDER", "") or ""
        if not judge_name or judge_name.strip().lower() == self.name:
            return True, []  # no separate judge (or would recurse) -> structural only

        try:
            from llm import routing

            provider = routing.get_provider(judge_name)
            verdict_text = provider.invoke_text(
                _JUDGE_SYSTEM,
                _build_judge_prompt(task, answer),
                256,
                provider.model_for_tier("cheap"),
                "cheap",
                usage,
            )
            verdict = salvage_json(strip_code_fence(verdict_text))
            if not isinstance(verdict, dict):
                raise ValueError("completeness judge did not return a JSON object")
            complete = bool(verdict.get("complete"))
            missing = [str(m) for m in (verdict.get("missing") or [])]
            return complete, missing
        except (
            Exception
        ) as e:  # noqa: BLE001 - judge is best-effort; don't loop forever
            logger.warning(
                "claude_agent completeness judge failed (%s); accepting answer", e
            )
            return True, []


_JUDGE_SYSTEM = (
    "You verify whether an assistant's ANSWER fully and correctly satisfies a TASK. "
    "You only check completeness and conformance to the requested JSON shape — you do "
    "NOT re-do the work. Reply with a single JSON object and nothing else."
)


def _build_judge_prompt(task, answer):
    return f"""TASK GIVEN TO THE WORKER:
{task[:20000]}

WORKER'S ANSWER:
{answer[:40000]}

Does the answer COMPLETELY satisfy the task and conform to the exact JSON shape it
asked for (all required keys present, no placeholders, nothing truncated)? Reply
EXACTLY: {{"complete": <true|false>, "missing": ["<short gap>", ...]}}"""
