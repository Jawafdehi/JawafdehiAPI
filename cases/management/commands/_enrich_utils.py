"""Shared utilities for CIAA enrichment management commands.

Reduces code duplication between enrich_ciaa_allegations.py,
enrich_ciaa_timeline.py, and future enrichment commands.
"""

import json
import logging
import os
import tempfile
import time
from datetime import date
from pathlib import Path
from typing import Optional, Union
from urllib.parse import urljoin, urlparse

import requests
from django.core.management.base import CommandError

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = frozenset(
    {"ciaa.gov.np", "ngm-store.jawafdehi.org", "s3.jawafdehi.org"}
)


def resolve_api_key(cli_key: Optional[str]) -> Optional[str]:
    """Resolve LLM API key from CLI argument or environment variables."""
    if cli_key:
        return cli_key
    return os.environ.get("JAWAFDEHI_LLM_API_KEY") or os.environ.get(
        "ANTHROPIC_API_KEY"
    )


def is_valid_iso_date(date_str: str) -> bool:
    """Validate that a string is a strictly formatted YYYY-MM-DD ISO date."""
    if not isinstance(date_str, str):
        return False
    candidate = date_str.strip()
    if len(candidate) != 10 or candidate[4] != "-" or candidate[7] != "-":
        return False
    try:
        date.fromisoformat(candidate)
        return True
    except (ValueError, TypeError):
        return False


def _parse_llm_response_body(raw: str) -> Optional[dict]:
    """Parse the HTTP response body from an LLM proxy, handling both:

    - Standard JSON: {"choices": [...], ...}
    - SSE/streaming format: data: {...}\\ndata: {...}\\ndata: [DONE]\\n

    For streaming responses, assembles content from delta chunks and returns
    a synthetic choices object so the rest of call_llm works unchanged.
    Returns None if the body cannot be parsed into a usable dict.
    """
    raw = raw.strip()
    if not raw:
        return None

    # Try plain JSON first (most common case)
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Try SSE streaming format: lines starting with "data: "
    if "data:" in raw:
        assembled_content = []
        final_chunk = None

        for line in raw.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue

            if not isinstance(chunk, dict):
                continue

            # Last non-DONE chunk may have finish_reason; keep it as template
            final_chunk = chunk

            # Accumulate delta content
            choices = chunk.get("choices", [])
            if choices and isinstance(choices[0], dict):
                delta = choices[0].get("delta", {})
                piece = delta.get("content", "")
                if piece:
                    assembled_content.append(piece)

        if assembled_content and final_chunk is not None:
            # Build a non-streaming choices object from assembled content
            full_content = "".join(assembled_content)
            synthetic = dict(final_chunk)
            synthetic["choices"] = [
                {
                    "message": {"role": "assistant", "content": full_content},
                    "finish_reason": "stop",
                    "index": 0,
                }
            ]
            return synthetic

    return None


def call_llm(
    system_prompt: str,
    user_prompt: str,
    model: str,
    base_url: str,
    api_key: Optional[str],
    session: requests.Session,
    max_retries: int = 3,
) -> str:
    """Call LLM API via OpenAI-compatible chat completions endpoint.

    Retry strategy:
    - ConnectionError: retry with backoff (proxy restart / transient blip).
    - ReadTimeout: retry with backoff up to max_retries — the proxy may be
      slow to respond on first hit (cold model) but succeed on retry.
      Each retry uses an incrementally larger read timeout (60s, 90s, 120s)
      so we fail fast on first attempt and give more headroom on retries.
    - 4xx: fail immediately (client error, retrying won't help).
    - 5xx: retry with backoff.

    Connect timeout is kept short (15s) since the proxy is always reachable;
    a long connect timeout just means a dead proxy hangs the whole run.
    """
    if max_retries < 1:
        raise ValueError("max_retries must be >= 1")

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 2000,  # enough for ~20-30 entities; bumped from 1500 for larger press-release-only cases
        "stream": False,  # explicitly disable streaming; proxy may default to SSE
    }

    data = _post_chat_completion(url, headers, payload, session, max_retries)
    message, finish_reason = _extract_message(data)
    return _require_text_content(message, finish_reason, data, payload["max_tokens"])


def _post_chat_completion(
    url: str,
    headers: dict,
    payload: dict,
    session: requests.Session,
    max_retries: int,
) -> dict:
    """POST a chat-completion request with retry/backoff and return parsed JSON.

    Handles transient connection/timeout/5xx errors with exponential backoff,
    fails fast on 4xx, and tolerates SSE-formatted bodies. Raises CommandError
    on terminal failure or unparseable response.
    """
    # Read timeouts per attempt: fail fast on attempt 1, give more time on retries.
    # This avoids burning 5 min × 3 attempts = 15 min on a slow proxy.
    read_timeouts = [60, 90, 120]

    last_exc = None
    for attempt in range(1, max_retries + 1):
        read_timeout = read_timeouts[min(attempt - 1, len(read_timeouts) - 1)]
        try:
            response = session.post(
                url,
                headers=headers,
                json=payload,
                timeout=(15, read_timeout),
            )
            response.raise_for_status()
        except requests.ConnectionError as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = 2**attempt
                logger.warning(
                    "LLM API connection failed (attempt %d/%d): %s. Retrying in %ds...",
                    attempt,
                    max_retries,
                    exc,
                    wait,
                )
                time.sleep(wait)
                continue
            raise CommandError(
                f"LLM API connection failed after {max_retries} attempts: {last_exc}"
            ) from exc
        except requests.Timeout as exc:
            last_exc = exc
            if attempt < max_retries:
                wait = 2**attempt
                logger.warning(
                    "LLM API request failed (attempt %d/%d): %s. Retrying in %ds...",
                    attempt,
                    max_retries,
                    exc,
                    wait,
                )
                time.sleep(wait)
                continue
            raise CommandError(
                f"LLM API timed out after {max_retries} attempts: {last_exc}"
            ) from exc
        except requests.HTTPError as exc:
            if exc.response is not None and 400 <= exc.response.status_code < 500:
                raise CommandError(
                    f"LLM API client error (HTTP {exc.response.status_code}): "
                    f"{exc.response.text[:500]}"
                ) from exc
            last_exc = exc
            if attempt < max_retries:
                wait = 2**attempt
                logger.warning(
                    "LLM API server error (attempt %d/%d): %s. Retrying in %ds...",
                    attempt,
                    max_retries,
                    exc,
                    wait,
                )
                time.sleep(wait)
                continue
            raise CommandError(
                f"LLM API failed after {max_retries} attempts: {last_exc}"
            ) from exc
        except requests.RequestException as exc:
            raise CommandError(f"LLM request failed: {exc}") from exc

        raw_body = response.text
        try:
            data = response.json()
        except (ValueError, TypeError) as exc:
            # The proxy may return a streaming response (SSE) even for non-streaming
            # requests, producing multiple JSON objects like:
            #   data: {...}\ndata: {...}\ndata: [DONE]\n
            # json() fails with "Extra data" in this case. Fall back to manual parsing.
            data = _parse_llm_response_body(raw_body)
            if data is None:
                raise CommandError(f"LLM API returned invalid JSON: {exc}") from exc

        if not isinstance(data, dict):
            raise CommandError("LLM API returned non-dictionary JSON root")
        return data

    # Unreachable: the loop either returns or raises on the final attempt.
    raise CommandError("LLM API request failed")  # pragma: no cover


def _extract_message(data: dict) -> tuple[dict, str]:
    """Return (message, finish_reason) from a chat-completion response."""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise CommandError("LLM API returned no choices")
    if not isinstance(choices[0], dict):
        raise CommandError("LLM API returned malformed choice object")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise CommandError("LLM API returned message missing or invalid")
    return message, choices[0].get("finish_reason", "")


def _require_text_content(
    message: dict, finish_reason: str, data: dict, max_tokens: int
) -> str:
    """Extract the assistant text content from a message, or raise CommandError."""
    content = message.get("content")

    # DeepSeek-R1 reasoning models sometimes put the answer in
    # reasoning_content when content is empty (proxy-dependent behaviour)
    if not content and message.get("reasoning_content"):
        content = message.get("reasoning_content")

    if content is None:
        if "refusal" in message:
            raise CommandError(
                f"LLM refused: {str(message['refusal'])[:200]}"
            ) from None
        if "tool_calls" in message:
            raise CommandError("LLM returned tool_calls instead of content") from None
        raise CommandError("LLM message missing required 'content' key") from None

    if not isinstance(content, str):
        raise CommandError(
            f"LLM content is not a string: {type(content).__name__}"
        ) from None

    if not content.strip():
        usage = data.get("usage", {})
        reasoning_tokens = usage.get("completion_tokens_details", {}).get(
            "reasoning_tokens", 0
        )
        if finish_reason == "length" and reasoning_tokens >= max_tokens - 50:
            raise CommandError(
                f"LLM reasoning model exhausted all {max_tokens} tokens "
                f"on internal reasoning ({reasoning_tokens} reasoning tokens) with "
                f"no tokens left for output. Increase max_tokens or simplify prompt."
            )
        logger.warning(
            "LLM API returned empty content. finish_reason=%s usage=%s",
            finish_reason,
            usage,
        )
        raise CommandError("LLM API returned empty content")

    return content


# ── date conversion tool (AD <-> Bikram Sambat) ──────────────────────────────

# OpenAI function-tool definition for the LLM. Mirrors the jawafdehi-mcp
# `convert_date` tool so the model can convert dates reliably instead of doing
# error-prone BS<->AD arithmetic in its head.
CONVERT_DATE_TOOL = {
    "type": "function",
    "function": {
        "name": "convert_date",
        "description": (
            "Convert dates between AD (Gregorian) and BS (Bikram Sambat) using "
            "Nepal's official calendar (Asia/Kathmandu). LLMs frequently get "
            "BS<->AD conversion wrong; always use this tool instead of "
            "converting in your head."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "dates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Dates to convert, each in YYYY-MM-DD format "
                        "(e.g. ['2023-01-15', '2079-10-01'])."
                    ),
                },
                "mode": {
                    "type": "string",
                    "enum": ["ad_to_bs", "bs_to_ad"],
                    "description": "Direction of conversion.",
                },
            },
            "required": ["dates", "mode"],
        },
    },
}

# Anthropic (Bedrock Messages API) tool schema — same tool, different envelope:
# Anthropic uses a flat {name, description, input_schema} shape rather than
# OpenAI's nested {type:"function", function:{...}}.
CONVERT_DATE_TOOL_ANTHROPIC = {
    "name": CONVERT_DATE_TOOL["function"]["name"],
    "description": CONVERT_DATE_TOOL["function"]["description"],
    "input_schema": CONVERT_DATE_TOOL["function"]["parameters"],
}

_DEVANAGARI_TO_ASCII_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def convert_date(dates: list, mode: str) -> dict:
    """Convert a list of dates between AD and BS using the `nepali` package.

    Returns a dict mapping each input date string to its converted value, or to
    an "Error: ..." string when that date cannot be converted. This is executed
    in-process (no network) and is the same calendar math the jawafdehi-mcp
    convert_date tool uses.
    """
    from nepali.datetime import nepalidate

    if mode not in ("ad_to_bs", "bs_to_ad"):
        raise ValueError("mode must be 'ad_to_bs' or 'bs_to_ad'")
    if not isinstance(dates, list):
        raise ValueError("dates must be a list of YYYY-MM-DD strings")

    results: dict[str, str] = {}
    for raw in dates:
        if not isinstance(raw, str):
            results[str(raw)] = "Error: date must be a YYYY-MM-DD string"
            continue
        normalized = (
            raw.strip().translate(_DEVANAGARI_TO_ASCII_DIGITS).replace("/", "-")
        )
        parts = normalized.split("-")
        if len(parts) != 3:
            results[raw] = "Error: date must be in YYYY-MM-DD format"
            continue
        try:
            year, month, day = (int(parts[0]), int(parts[1]), int(parts[2]))
            if mode == "ad_to_bs":
                import datetime as _dt

                converted = nepalidate.from_date(_dt.date(year, month, day)).strftime(
                    "%Y-%m-%d"
                )
            else:
                converted = (
                    nepalidate(year, month, day)
                    .to_datetime()
                    .date()
                    .strftime("%Y-%m-%d")
                )
            results[raw] = converted
        except Exception as exc:  # noqa: BLE001
            # The `nepali` package raises its own exception types (e.g.
            # FormatNotMatchException) that subclass neither ValueError nor
            # TypeError; report the per-date error rather than aborting the run.
            results[raw] = f"Error: {exc}"
    return results


def call_llm_with_tools(
    system_prompt: str,
    user_prompt: str,
    model: str,
    base_url: str,
    api_key: Optional[str],
    session: requests.Session,
    tools: list,
    tool_executors: dict,
    max_tokens: int = 4000,
    max_tool_rounds: int = 30,
    max_retries: int = 3,
) -> str:
    """Call the LLM with OpenAI function-tools, running a tool-use loop.

    The model may emit tool_calls; each is dispatched to ``tool_executors[name]``
    (a callable taking the parsed JSON arguments and returning a JSON-serialisable
    result), the result is appended to the conversation, and the request is
    repeated until the model returns final text content. The loop is bounded by
    ``max_tool_rounds`` to prevent runaway tool use.

    Returns the model's final text content. Raises CommandError on failure or if
    the tool-round budget is exhausted.
    """
    if max_tool_rounds < 1:
        raise ValueError("max_tool_rounds must be >= 1")

    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    for _ in range(max_tool_rounds):
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.1,
            "max_tokens": max_tokens,
            "stream": False,
            "tools": tools,
            "tool_choice": "auto",
        }
        data = _post_chat_completion(url, headers, payload, session, max_retries)
        message, finish_reason = _extract_message(data)

        tool_calls = message.get("tool_calls")
        if not tool_calls:
            # No tool calls — this is the final answer.
            return _require_text_content(message, finish_reason, data, max_tokens)

        # Echo the assistant turn (with its tool_calls) before the tool results.
        messages.append(
            {
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            }
        )
        for call in tool_calls:
            messages.append(_run_tool_call(call, tool_executors))

    raise CommandError(
        f"LLM exceeded the maximum of {max_tool_rounds} tool-use rounds "
        "without producing a final answer."
    )


def _run_tool_call(call: dict, tool_executors: dict) -> dict:
    """Execute one tool_call and return the 'tool' role message to append."""
    call_id = call.get("id", "")
    function = call.get("function") or {}
    name = function.get("name", "")
    raw_args = function.get("arguments") or "{}"

    def _tool_message(content: str) -> dict:
        return {"role": "tool", "tool_call_id": call_id, "content": content}

    executor = tool_executors.get(name)
    if executor is None:
        return _tool_message(f"Error: unknown tool '{name}'")
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        result = executor(**args)
        return _tool_message(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        # Any tool failure is reported back to the model as a tool result rather
        # than aborting the whole enrichment run; the model can then recover or
        # proceed without that tool's output.
        logger.warning("Tool '%s' failed: %s", name, exc)
        return _tool_message(f"Error: {exc}")


def call_bedrock_with_tools(
    system_prompt: str,
    user_prompt: str,
    model_id: str,
    tools: list,
    tool_executors: dict,
    aws_profile: str = "",
    aws_region: str = "us-west-2",
    max_tokens: int = 4000,
    max_tool_rounds: int = 30,
) -> str:
    """Call a Claude model on AWS Bedrock (native Messages API) with a tool-use loop.

    This is the Bedrock-native counterpart to ``call_llm_with_tools`` — Claude
    (e.g. Opus 4.8) is reached through ``bedrock-runtime.invoke_model`` rather
    than an OpenAI-compatible gateway. The model may emit ``tool_use`` content
    blocks; each is dispatched to ``tool_executors[name]`` and returned as a
    ``tool_result`` block in a follow-up user turn until the model stops with a
    final text answer. Bounded by ``max_tool_rounds``.

    ``tools`` must use the Anthropic schema (name/description/input_schema), e.g.
    ``CONVERT_DATE_TOOL_ANTHROPIC``. Returns the model's final text content.
    """
    if max_tool_rounds < 1:
        raise ValueError("max_tool_rounds must be >= 1")

    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    session = boto3.Session(profile_name=aws_profile or None, region_name=aws_region)
    client = session.client(
        "bedrock-runtime",
        config=Config(
            read_timeout=120,
            connect_timeout=15,
            # Adaptive mode adds client-side rate-limiting + backoff on
            # ThrottlingException, which matters when several enrichment workers
            # call Bedrock concurrently (see enrich_ciaa_description --concurrency).
            retries={"max_attempts": 6, "mode": "adaptive"},
        ),
    )

    messages = [{"role": "user", "content": user_prompt}]

    for _ in range(max_tool_rounds):
        body = {
            # Note: no "temperature" — deprecated for newer Bedrock Claude models
            # (e.g. Opus 4.8), which reject it with a ValidationException.
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": max_tokens,
            "system": system_prompt,
            "tools": tools,
            "messages": messages,
        }
        try:
            resp = client.invoke_model(modelId=model_id, body=json.dumps(body))
            payload = json.loads(resp["body"].read())
        except (BotoCoreError, ClientError, ValueError, KeyError) as exc:
            raise CommandError(f"Bedrock invoke_model failed: {exc}") from exc

        content_blocks = payload.get("content") or []
        stop_reason = payload.get("stop_reason")

        if stop_reason != "tool_use":
            # Final answer — concatenate any text blocks.
            text = "".join(
                b.get("text", "")
                for b in content_blocks
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            if not text:
                raise CommandError("Bedrock returned empty content")
            return text

        # Echo the assistant turn, then answer each tool_use with a tool_result.
        messages.append({"role": "assistant", "content": content_blocks})
        tool_results = []
        for block in content_blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_results.append(_run_bedrock_tool_use(block, tool_executors))
        messages.append({"role": "user", "content": tool_results})

    raise CommandError(
        f"Bedrock model exceeded the maximum of {max_tool_rounds} tool-use rounds "
        "without producing a final answer."
    )


def _run_bedrock_tool_use(block: dict, tool_executors: dict) -> dict:
    """Execute one Anthropic tool_use block; return the tool_result content block."""
    use_id = block.get("id", "")
    name = block.get("name", "")
    args = block.get("input") or {}

    def _result(content: str, is_error: bool = False) -> dict:
        out = {"type": "tool_result", "tool_use_id": use_id, "content": content}
        if is_error:
            out["is_error"] = True
        return out

    executor = tool_executors.get(name)
    if executor is None:
        return _result(f"Error: unknown tool '{name}'", is_error=True)
    try:
        result = executor(**args) if isinstance(args, dict) else executor(args)
        return _result(json.dumps(result, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Tool '%s' failed: %s", name, exc)
        return _result(f"Error: {exc}", is_error=True)


def convert_to_markdown(url: str, session: requests.Session) -> Optional[str]:
    """Download file from URL and convert to markdown using likhit.

    Pipeline: URL download -> temp file -> likhit/markitdown -> markdown.
    Returns None when conversion fails or produces insufficient content.
    """
    initial_hostname = urlparse(url).hostname
    if initial_hostname not in ALLOWED_HOSTS:
        logger.warning("Refusing to fetch untrusted host: %s", url)
        return None

    current_url = url
    for hop in range(5):
        try:
            response = session.get(
                current_url, timeout=120, stream=True, allow_redirects=False
            )
        except requests.RequestException as exc:
            logger.warning("Failed to download %s: %s", current_url, exc)
            return None

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("Location")
            if not location:
                logger.warning("Redirect with no Location header: %s", current_url)
                return None
            next_url = urljoin(current_url, location)
            next_hostname = urlparse(next_url).hostname
            if next_hostname not in ALLOWED_HOSTS:
                logger.warning(
                    "Redirect target host not allowed: %s -> %s",
                    current_url,
                    next_url,
                )
                return None
            current_url = next_url
            continue

        response.raise_for_status()
        break
    else:
        logger.warning("Too many redirects: %s", url)
        return None

    content_type = response.headers.get("content-type", "").lower()

    if "text/plain" in content_type or "application/json" in content_type:
        response.encoding = "utf-8"
        text = response.text
        if len(text) > 200:
            return text
        return None

    suffix = ""
    if "pdf" in content_type:
        suffix = ".pdf"
    elif "html" in content_type:
        suffix = ".html"
    elif any(kw in content_type for kw in ("document", "word", "docx", "msword", "cfb")):
        suffix = ".doc"

    # Fall back to the URL's own file extension when content-type is unhelpful
    # (e.g. S3/nginx serving .doc files as application/x-cfb or octet-stream).
    if not suffix:
        url_suffix = Path(urlparse(current_url).path).suffix.lower()
        if url_suffix in (".doc", ".docx", ".pdf", ".html", ".htm", ".txt"):
            suffix = url_suffix

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp_path = tmp.name
            for chunk in response.iter_content(chunk_size=8192):
                tmp.write(chunk)

        import likhit  # noqa: F401
        from markitdown import MarkItDown

        md = MarkItDown(enable_plugins=True)
        result = md.convert(tmp_path)

        if result and result.text_content and len(result.text_content.strip()) > 200:
            return result.text_content.strip()

        logger.warning("Likhit conversion produced insufficient content for %s", url)
        return None
    except Exception as exc:
        logger.warning("Likhit conversion failed for %s: %s", url, exc)
        return None
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass


def parse_extraction_response(
    response_text: str, wrapper_keys: set[str]
) -> Optional[Union[list, list[dict]]]:
    """Extract a JSON array from an LLM response, handling markdown wrappers.

    Handles:
    - Bare JSON array: [...]
    - Wrapped object: {"entities": [...]}
    - Markdown code fences: ```json ... ```
    - Extra text before/after the JSON block

    Returns the raw parsed array; caller handles field mapping.
    Returns None when parsing fails or array is empty.
    """
    text = response_text.strip()

    # Strip markdown code fences if present (str.find = O(n), no ReDoS)
    if "```" in text:
        start = text.find("```")
        if start != -1:
            nl = text.find("\n", start)
            if nl != -1:
                end = text.find("```", nl)
                if end != -1:
                    text = text[nl + 1 : end].strip()

    # Strategy 1: try to find a JSON object wrapper {"entities": [...]}
    # and extract the array directly — avoids "Extra data" from trailing braces
    obj_start = text.find("{")
    if obj_start != -1:
        # Find matching closing brace
        depth = 0
        obj_end = -1
        for i, ch in enumerate(text[obj_start:], obj_start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    obj_end = i
                    break
        if obj_end != -1:
            try:
                obj = json.loads(text[obj_start : obj_end + 1])
                if isinstance(obj, dict):
                    for wrapper_key in wrapper_keys:
                        if isinstance(obj.get(wrapper_key), list):
                            entries = obj[wrapper_key]
                            if entries:
                                return entries
            except json.JSONDecodeError:
                pass  # fall through to array strategy

    # Strategy 2: find a bare JSON array [...]
    arr_start = text.find("[")
    if arr_start != -1:
        # Find matching closing bracket
        depth = 0
        arr_end = -1
        for i, ch in enumerate(text[arr_start:], arr_start):
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    arr_end = i
                    break
        if arr_end != -1:
            try:
                entries = json.loads(text[arr_start : arr_end + 1])
                if isinstance(entries, list) and entries:
                    return entries
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse JSON array from LLM response: %s", exc)
                logger.debug("JSON string: %s", text[arr_start : arr_end + 1][:500])

    logger.warning("Could not extract JSON from LLM response")
    logger.debug("Response: %s", text[:500])
    return None
