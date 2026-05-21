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

ALLOWED_HOSTS = frozenset({"ciaa.gov.np", "ngm-store.jawafdehi.org"})


def resolve_api_key(
    cli_key: Optional[str] = None, is_anthropic: bool = False
) -> Optional[str]:
    """Resolve LLM API key from CLI argument or environment variables.

    When is_anthropic is True, prefers ANTHROPIC_API_KEY first.
    Otherwise prefers JAWAFDEHI_LLM_API_KEY and OPENCODE_API_KEY first.
    """
    if cli_key and cli_key.strip():
        return cli_key.strip()
    if is_anthropic:
        for var in ("ANTHROPIC_API_KEY", "JAWAFDEHI_LLM_API_KEY", "OPENCODE_API_KEY"):
            val = os.environ.get(var)
            if val:
                return val
    else:
        for var in ("JAWAFDEHI_LLM_API_KEY", "OPENCODE_API_KEY", "ANTHROPIC_API_KEY"):
            val = os.environ.get(var)
            if val:
                return val
    return None


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

    Implements retry with exponential backoff for transient failures.
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
        "max_tokens": 4000,
    }

    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            response = session.post(url, headers=headers, json=payload, timeout=120)
            response.raise_for_status()
        except requests.RequestException as exc:
            last_exc = exc
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                status = exc.response.status_code
                if 400 <= status < 500:
                    raise CommandError(
                        f"LLM API client error (HTTP {status}): "
                        f"{exc.response.text[:500]}"
                    ) from exc
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
                f"LLM API request failed after {max_retries} attempts: {last_exc}"
            ) from exc

        try:
            data = response.json()
        except (ValueError, TypeError) as exc:
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

        content = message.get("content")
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
            raise CommandError("LLM API returned empty content")

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

    Handles markdown code fences and nested wrapper keys.
    Returns the raw parsed array; caller handles field mapping.
    Returns None when parsing fails or array is empty.
    """
    text = response_text.strip()

    json_start = text.find("[")
    json_end = text.rfind("]")

    if json_start == -1 or json_end == -1 or json_end <= json_start:
        logger.warning("Could not find JSON array in LLM response")
        logger.debug("Response: %s", text[:500])
        return None

    json_str = text[json_start : json_end + 1]

    try:
        entries = json.loads(json_str)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse JSON from LLM response: %s", exc)
        logger.debug("JSON string: %s", json_str[:500])
        return None

    if isinstance(entries, dict):
        for wrapper_key in wrapper_keys:
            if isinstance(entries.get(wrapper_key), list):
                entries = entries[wrapper_key]
                break

    if not isinstance(entries, list):
        logger.warning("LLM returned non-list: %s", type(entries).__name__)
        return None

    if not entries:
        return None

    return entries
