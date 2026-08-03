"""Subprocess-based CLI providers for offline/local LLM harnesses.

Invokes locally-installed CLIs (claude, codex) authenticated by the user's
SUBSCRIPTION, not API keys. Parses CLI output formats exactly.
"""

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time

from django.conf import settings

from llm.providers.base import Provider, strip_code_fence

def _flatten(content):
    """Flatten content to plain text for CLI input.

    Args:
        content: A string OR a list of {"type":"text","text":...} blocks
            (e.g. from cache_control prompt blocks).

    Returns:
        Plain text string.
    """
    if isinstance(content, str):
        return content
    return "\n\n".join(b.get("text", "") for b in content)


def _dominant_model(model_usage):
    """Pick the model that did the real work from claude -p's ``modelUsage`` map.

    A single ``claude -p`` call can report usage for MORE than one model — the
    model we requested via ``--model`` plus an auxiliary the CLI drives itself
    for internal housekeeping (e.g. a small model for a quota/title side-task).
    Taking ``list(modelUsage)[0]`` mislabels the usage bucket with whichever key
    hashes first, which can be the auxiliary model even though the requested
    model produced the entire graded result (and the flat ``total_cost_usd``
    already reflects it). Choose the model with the highest reported cost —
    tie-broken by token count — so the bucket label names the actual grader.
    Cost is the primary key because per-model ``inputTokens`` excludes cache
    tokens, so a cache-heavy primary call can report fewer counted tokens than a
    tiny uncached auxiliary one while still costing far more. Returns "" when
    ``modelUsage`` is absent/empty/malformed so the caller can fall back.
    """
    if not isinstance(model_usage, dict) or not model_usage:
        return ""

    def weight(item):
        _name, u = item
        u = u if isinstance(u, dict) else {}
        cost = float(u.get("costUSD") or 0.0)
        tokens = int(u.get("inputTokens") or 0) + int(u.get("outputTokens") or 0)
        return (cost, tokens)

    return max(model_usage.items(), key=weight)[0]


class _CliProvider(Provider):
    """Base class for subprocess-based CLI providers.

    Handles subprocess invocation with retries for rate limits and timeouts.
    """

    def _run(self, argv, stdin_text, env):
        """Run a CLI subprocess with retry logic for rate limits.

        Args:
            argv: Command line (list of strings).
            stdin_text: Input to pass via stdin.
            env: Environment dict for subprocess.

        Returns:
            stdout text if successful.

        Raises:
            RuntimeError: If command fails or retries exhausted.
        """
        max_retries = int(getattr(settings, "REVIEW_CLI_MAX_RETRIES", 3))
        timeout = int(getattr(settings, "REVIEW_CLI_TIMEOUT", 300))

        # One scratch cwd reused across retries (CLIs may write to it), always
        # cleaned up so a long-lived poller doesn't leak temp dirs.
        cwd = tempfile.mkdtemp(prefix="llmcli-")
        try:
            for attempt in range(max_retries):
                try:
                    proc = subprocess.run(
                        argv,
                        input=stdin_text,
                        capture_output=True,
                        text=True,
                        timeout=timeout,
                        env=env,
                        cwd=cwd,
                    )
                    combined = proc.stdout + "\n" + proc.stderr
                    if proc.returncode == 0:
                        return proc.stdout
                    lower = combined.lower()
                    retryable = any(
                        s in lower
                        for s in (
                            "rate limit",
                            "rate_limit",
                            "limit reached",
                            "usage limit",
                            "429",
                            "too many requests",
                            # Transient, and NOT a rate limit: an answer that
                            # runs long asks to continue, and denying it aborts
                            # the call with an empty result. `CLAUDE_CLI_MAX_TURNS`
                            # makes that rare, not impossible -- a long enough
                            # answer still exhausts whatever the cap is, and the
                            # next attempt often lands inside it. Without this,
                            # every survivor is a permanently recorded error:
                            # measured at 3/10 on bigo-extraction prompts when
                            # the cap was 1.
                            #
                            # `llm/exhaustion.py` records the other half -- the
                            # same marker also fires on a genuinely exhausted
                            # token budget, and the two are indistinguishable
                            # from the error text. Retrying is the right move
                            # either way: a budget problem costs one wasted
                            # attempt, where not retrying costs the case.
                            "error_max_turns",
                            "maximum number of turns",
                        )
                    )
                    if retryable and attempt < max_retries - 1:
                        time.sleep(min(60, (2**attempt) + random.random()))
                        continue
                    raise RuntimeError(
                        f"{self.name} failed (rc={proc.returncode}): {combined[-500:]}"
                    )
                except subprocess.TimeoutExpired:
                    if attempt < max_retries - 1:
                        time.sleep(min(60, (2**attempt) + random.random()))
                        continue
                    raise RuntimeError(
                        f"{self.name}: command timed out after {timeout}s "
                        "(exhausted retries)"
                    )
            raise RuntimeError(f"{self.name}: exhausted {max_retries} retries")
        finally:
            shutil.rmtree(cwd, ignore_errors=True)


class ClaudeCliProvider(_CliProvider):
    """Invoke claude CLI with local authentication (subscription-based).

    The claude CLI uses SUBSCRIPTION auth (no API keys in env). It outputs
    a single JSON object to stdout with usage and result fields.
    """

    name = "claude_cli"
    supports_tools = True

    def _effort_args(self):
        """`--effort <level>` (low/medium/high/xhigh/max) when configured; the CLI
        reasoning budget. Empty -> CLI default."""
        eff = getattr(settings, "CLAUDE_CLI_EFFORT", "")
        return ["--effort", eff] if eff else []

    def _claude_env(self, max_tokens=None):
        """Env for the claude subprocess: subscription auth (no API key).

        The claude CLI has no output-cap flag; it honors the
        CLAUDE_CODE_MAX_OUTPUT_TOKENS env var, so the caller's ``max_tokens``
        budget is enforced through it — otherwise the judge's token budgeting
        would be silently ignored on this provider.
        """
        env = dict(os.environ)
        cli_home = getattr(settings, "CLAUDE_CLI_HOME", "")
        if cli_home:
            env["HOME"] = cli_home
        if max_tokens:
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(int(max_tokens))
        env.pop("ANTHROPIC_API_KEY", None)
        return env

    def _finalize(self, out, model_id, effective_model, tier, usage):
        """Parse claude -p --output-format json output; record usage; return text."""
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"claude_cli: failed to parse JSON output: {e}\nOutput: {out[-500:]}"
            )
        if not isinstance(data, dict):
            raise RuntimeError(f"claude_cli: output is not a JSON object: {out[-500:]}")
        if data.get("is_error") or "result" not in data:
            raise RuntimeError(
                f"claude_cli error: {data.get('result', 'unknown error')}\n"
                f"Payload: {out[-500:]}"
            )
        if usage is not None:
            usage_data = data.get("usage", {})
            model_usage = data.get("modelUsage", {})
            reported_model = (
                _dominant_model(model_usage) or effective_model or model_id or "claude"
            )
            input_tokens = (
                usage_data.get("input_tokens", 0)
                + usage_data.get("cache_creation_input_tokens", 0)
                + usage_data.get("cache_read_input_tokens", 0)
            )
            usage.add(
                input_tokens,
                usage_data.get("output_tokens", 0),
                provider="claude_cli",
                tier=tier,
                model=reported_model,
                cost_usd=data.get("total_cost_usd", 0.0),
            )
        return strip_code_fence(data["result"])

    def invoke_text(self, system, content, max_tokens, model_id, tier, usage=None):
        """Invoke claude -p --output-format json.

        Args:
            system: System prompt text.
            content: User message (string or cache_control blocks).
            max_tokens: Max tokens in response.
            model_id: Model identifier (may be overridden by settings).
            tier: "premium" or "cheap".
            usage: Optional UsageAccumulator to record tokens.

        Returns:
            Stripped response text (from data["result"]).

        Raises:
            RuntimeError: If invocation fails or output is malformed.
        """
        # NB: no --permission-mode plan. Plan mode makes claude -p emit a plan and
        # then try to ExitPlanMode (a tool turn), which with no tools available
        # aborts as error_max_turns before producing a result. Tools are already
        # disabled via --allowedTools "", so default mode never prompts.
        #
        # --max-turns was hardcoded to 1 here, on the reading that one turn is one
        # answer. It is not. A turn that runs long asks to continue, and a cap of 1
        # refuses — aborting the whole call as error_max_turns and returning
        # nothing, having already billed for the work done. The failure names the
        # turn limit but reads like an exhausted token budget, which is how it was
        # twice misdiagnosed (see llm/exhaustion.py).
        #
        # Measured on case_proposal.intent, identical payload and budget, arms
        # interleaved (2026-08-03): 3/5 succeeded at 1 turn, 5/5 at 3. Successful
        # 3-turn runs averaged ~25s against ~15s for successful 1-turn runs, so the
        # extra turn is genuinely used rather than idle.
        #
        # Independently reproduced on a second payload -- casework bigo extraction
        # (sonnet, same case + prompt, n=10 per arm): 3 failures / 10 at 1 turn,
        # 0 / 10 at 2, all ten agreeing on the same extracted amount. Different
        # prompt, different tier, same mechanism. Default mode is not immune
        # either: the model spends a turn *attempting* a tool call even when none
        # are exposed.
        #
        # Raising the cap does not make a short call longer or dearer: a response
        # that completes in one turn never requests a second. It only stops
        # long-running answers from being thrown away.
        argv = [
            getattr(settings, "CLAUDE_CLI_BIN", "claude"),
            "-p",
            "--output-format",
            "json",
            "--system-prompt",
            system,
            "--max-turns",
            str(max(1, getattr(settings, "CLAUDE_CLI_MAX_TURNS", 3))),
            "--allowedTools",
            "",
        ]

        # model_id is already routing.invoke_text's resolved model_for_tier(tier).
        effective_model = model_id
        if effective_model:
            argv.extend(["--model", effective_model])
        argv.extend(self._effort_args())

        out = self._run(argv, _flatten(content), self._claude_env(max_tokens))
        return self._finalize(out, model_id, effective_model, tier, usage)

    def invoke_with_tools(
        self,
        system,
        content,
        max_tokens,
        model_id,
        tier,
        tools,
        usage=None,
        max_iterations=8,
    ):
        """Real tool-use for claude -p: expose `tools` via a stdio MCP server.

        claude -p can only call custom tools through MCP, so we launch
        llm.cli_mcp_server with the given tools (those that carry a run_path) and
        allow exactly those tool names. max_iterations becomes the turn budget so
        the model can call a tool and then answer. Tools without a run_path can't
        be exposed to a subprocess, so we fall back to a no-tool call.
        """
        exposable = [t for t in tools if getattr(t, "run_path", None)]
        if not exposable:
            return self.invoke_text(system, content, max_tokens, model_id, tier, usage)

        registry = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
                "run_path": t.run_path,
            }
            for t in exposable
        ]
        # repo root holds both `llm` and the tools' packages (e.g. casework).
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        tmpdir = tempfile.mkdtemp(prefix="llmcli-mcp-")
        try:
            reg_path = os.path.join(tmpdir, "tools.json")
            with open(reg_path, "w") as f:
                json.dump(registry, f, ensure_ascii=False)
            mcp_cfg = {
                "mcpServers": {
                    "llmtools": {
                        "command": sys.executable,
                        "args": ["-m", "llm.cli_mcp_server"],
                        "env": {
                            "LLM_CLI_TOOLS_REGISTRY": reg_path,
                            "LLM_CLI_TOOLS_PYPATH": repo_root,
                            "PYTHONPATH": repo_root,
                        },
                    }
                }
            }
            cfg_path = os.path.join(tmpdir, "mcp.json")
            with open(cfg_path, "w") as f:
                json.dump(mcp_cfg, f)

            argv = [
                getattr(settings, "CLAUDE_CLI_BIN", "claude"),
                "-p",
                "--output-format",
                "json",
                "--system-prompt",
                system,
                "--max-turns",
                str(max(2, max_iterations)),
                "--mcp-config",
                cfg_path,
                "--strict-mcp-config",
                *self._effort_args(),
                "--allowedTools",
                *[f"mcp__llmtools__{t.name}" for t in exposable],
            ]
            effective_model = model_id
            if effective_model:
                argv[1:1] = ["--model", effective_model]

            out = self._run(argv, _flatten(content), self._claude_env(max_tokens))
            return self._finalize(out, model_id, effective_model, tier, usage)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def model_for_tier(self, tier):
        """Resolve tier to a claude CLI model id/alias.

        Premium (gate) and cheap (bulk) tiers can use different models — e.g.
        "opus" for gates, "sonnet"/"haiku" for routine — via
        CLAUDE_CLI_MODEL_PREMIUM / CLAUDE_CLI_MODEL_CHEAP. Falls back to the
        single CLAUDE_CLI_MODEL_ID, then to "" (CLI default). The claude CLI
        accepts aliases ("opus"/"sonnet"/"haiku") or full model ids.
        """
        fallback = getattr(settings, "CLAUDE_CLI_MODEL_ID", "")
        if tier == "premium":
            return getattr(settings, "CLAUDE_CLI_MODEL_PREMIUM", "") or fallback
        return getattr(settings, "CLAUDE_CLI_MODEL_CHEAP", "") or fallback


class CodexCliProvider(_CliProvider):
    """Invoke codex exec CLI with local authentication (subscription-based).

    The codex CLI has no system-prompt flag, so we prepend the system prompt
    to the input. It outputs JSONL (one JSON object per line) with usage and
    agent-message events.
    """

    name = "codex_cli"

    def invoke_text(self, system, content, max_tokens, model_id, tier, usage=None):
        """Invoke codex exec --json.

        Args:
            system: System prompt text (prepended to content).
            content: User message (string or cache_control blocks).
            max_tokens: Max tokens in response.
            model_id: Model identifier (may be overridden by settings).
            tier: "premium" or "cheap".
            usage: Optional UsageAccumulator to record tokens.

        Returns:
            Stripped response text (from last agent_message item.text).

        Raises:
            RuntimeError: If invocation fails, no agent message found, or output malformed.
        """
        # Prepend system to prompt (codex has no system-prompt flag)
        prompt = system + "\n\n" + _flatten(content)

        argv = [
            getattr(settings, "CODEX_CLI_BIN", "codex"),
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
        ]

        # model_id is already routing.invoke_text's resolved model_for_tier(tier).
        effective_model = model_id
        if effective_model:
            argv.extend(["--model", effective_model])

        # Read prompt from stdin
        argv.append("-")

        # Prepare environment (use CODEX_HOME if set, remove API key)
        env = dict(os.environ)
        codex_home = getattr(settings, "CODEX_HOME", "")
        if codex_home:
            env["CODEX_HOME"] = codex_home
        env.pop("OPENAI_API_KEY", None)

        # Run subprocess
        out = self._run(argv, prompt, env)

        # Parse JSONL output
        last_agent_message = None
        turn_usage = None
        lines = out.strip().split("\n")

        for line in lines:
            line = line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # Skip non-JSON lines (e.g. "Reading additional input from stdin...")
                continue
            if not isinstance(event, dict):
                continue

            # Look for item.completed events with agent_message
            if event.get("type") == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    last_agent_message = item.get("text", "")

            # Look for turn.completed with usage
            if event.get("type") == "turn.completed":
                turn_usage = event.get("usage", {})

        if not last_agent_message:
            raise RuntimeError(
                f"codex_cli: no agent message found in output\nLast 500 chars: {out[-500:]}"
            )

        # Record usage even when codex emitted no turn.completed event (or
        # omitted counts): a zero-token record keeps the CALL count accurate
        # instead of silently dropping the invocation from the accounting.
        if usage is not None:
            turn_usage = turn_usage or {}
            input_tokens = turn_usage.get("input_tokens", 0)
            output_tokens = turn_usage.get("output_tokens", 0)
            reasoning_output_tokens = turn_usage.get("reasoning_output_tokens", 0)

            usage.add(
                input_tokens,
                output_tokens + reasoning_output_tokens,
                provider="codex_cli",
                tier=tier,
                model=effective_model or "codex",
                cost_usd=0.0,  # Subscription-based, no per-call cost
            )

        return strip_code_fence(last_agent_message)

    def model_for_tier(self, tier):
        """Resolve tier to codex CLI model id.

        Returns configured CODEX_MODEL_ID (may be empty, allowing CLI default).
        """
        return getattr(settings, "CODEX_MODEL_ID", "")
