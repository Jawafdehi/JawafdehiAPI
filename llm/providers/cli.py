"""Subprocess-based CLI providers for offline/local LLM harnesses.

Invokes locally-installed CLIs (claude, codex) authenticated by the user's
SUBSCRIPTION, not API keys. Parses CLI output formats exactly.
"""

import json
import os
import random
import shutil
import subprocess
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
        argv = [
            getattr(settings, "CLAUDE_CLI_BIN", "claude"),
            "-p",
            "--output-format",
            "json",
            "--system-prompt",
            system,
            "--max-turns",
            "1",
            "--allowedTools",
            "",
            "--permission-mode",
            "plan",
        ]

        # model_id is already routing.invoke_text's resolved model_for_tier(tier).
        effective_model = model_id
        if effective_model:
            argv.extend(["--model", effective_model])

        # Prepare environment (use CLAUDE_CLI_HOME if set, remove API key)
        env = dict(os.environ)
        cli_home = getattr(settings, "CLAUDE_CLI_HOME", "")
        if cli_home:
            env["HOME"] = cli_home
        env.pop("ANTHROPIC_API_KEY", None)

        # Run subprocess
        out = self._run(argv, _flatten(content), env)

        # Parse output
        try:
            data = json.loads(out)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"claude_cli: failed to parse JSON output: {e}\nOutput: {out[-500:]}"
            )

        # Check for errors
        if data.get("is_error") or "result" not in data:
            raise RuntimeError(
                f"claude_cli error: {data.get('result', 'unknown error')}\nPayload: {out[-500:]}"
            )

        # Record usage
        if usage is not None:
            usage_data = data.get("usage", {})
            model_usage = data.get("modelUsage", {})
            reported_model = (
                list(model_usage.keys())[0]
                if model_usage
                else (effective_model or model_id or "claude")
            )

            input_tokens = (
                usage_data.get("input_tokens", 0)
                + usage_data.get("cache_creation_input_tokens", 0)
                + usage_data.get("cache_read_input_tokens", 0)
            )
            output_tokens = usage_data.get("output_tokens", 0)
            cost_usd = data.get("total_cost_usd", 0.0)

            usage.add(
                input_tokens,
                output_tokens,
                provider="claude_cli",
                tier=tier,
                model=reported_model,
                cost_usd=cost_usd,
            )

        return strip_code_fence(data["result"])

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

        # Record usage (turn_usage may be {} if codex omitted counts -> still
        # records the call with 0 tokens rather than dropping it).
        if usage is not None and turn_usage is not None:
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
