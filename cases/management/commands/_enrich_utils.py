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

from cases.models import DocumentSource

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = frozenset({"ciaa.gov.np", "ngm-store.jawafdehi.org"})

DOCUMENT_FORMAT_PRIORITY = {".docx": 4, ".doc": 3, ".pdf": 2}


def rank_source_urls(source: DocumentSource) -> list[str]:
    """Return URLs sorted by format priority (DOCX > DOC > PDF > web)."""
    urls = [
        url.strip()
        for url in (source.url or [])
        if isinstance(url, str) and url.strip()
    ]
    if not urls:
        return []

    scored = []
    for url in urls:
        parsed = urlparse(url)
        suffix = Path(parsed.path).suffix.lower()
        priority = DOCUMENT_FORMAT_PRIORITY.get(suffix, 0)
        scored.append((priority, url))

    scored.sort(key=lambda x: x[0], reverse=True)

    seen = set()
    result = []
    for _priority, url in scored:
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def extract_court_case_number(case) -> str:
    """Extract a human-readable court case number (e.g. #080-CR-0007) from case.court_cases."""
    if not case.court_cases or not isinstance(case.court_cases, list):
        return ""
    for entry in case.court_cases:
        if isinstance(entry, str):
            parts = entry.split(":")
            case_number = parts[-1] if ":" in entry else entry
            if case_number:
                return f"#{case_number}"
    return ""


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

        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise CommandError("LLM API returned no choices")

        if not isinstance(choices[0], dict):
            raise CommandError("LLM API returned malformed choice object")

        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise CommandError("LLM API returned message missing or invalid")

        finish_reason = choices[0].get("finish_reason", "")

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
                raise CommandError(
                    "LLM returned tool_calls instead of content"
                ) from None
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
            if (
                finish_reason == "length"
                and reasoning_tokens >= payload["max_tokens"] - 50
            ):
                raise CommandError(
                    f"LLM reasoning model exhausted all {payload['max_tokens']} tokens "
                    f"on internal reasoning ({reasoning_tokens} reasoning tokens) with "
                    f"no tokens left for output. Increase max_tokens or simplify prompt."
                )
            if attempt < max_retries:
                wait = 2**attempt
                logger.warning(
                    "LLM returned empty content (attempt %d/%d). Retrying in %ds... "
                    "finish_reason=%s usage=%s",
                    attempt,
                    max_retries,
                    wait,
                    finish_reason,
                    usage,
                )
                time.sleep(wait)
                continue
            logger.warning(
                "LLM API returned empty content after %d attempts. finish_reason=%s usage=%s",
                max_retries,
                finish_reason,
                usage,
            )
            raise CommandError(
                f"LLM API returned empty content after {max_retries} attempts"
            ) from None

        return content


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
    elif any(kw in content_type for kw in ("document", "word", "docx", "msword")):
        suffix = ".docx"

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
