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
from urllib.parse import urlparse

import requests
from django.core.management.base import CommandError

logger = logging.getLogger(__name__)

ALLOWED_HOSTS = frozenset({"ciaa.gov.np", "ngm-store.jawafdehi.org"})


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
            if attempt < max_retries:
                wait = 2**attempt
                logger.warning(
                    "LLM API request failed (attempt %d/%d): %s. " "Retrying in %ds...",
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
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            raise CommandError("LLM API returned a malformed response") from exc

        if not content:
            raise CommandError("LLM API returned empty content")

        return content

    raise CommandError(
        f"LLM API request failed after {max_retries} attempts: {last_exc}"
    )


def convert_to_markdown(url: str, session: requests.Session) -> Optional[str]:
    """Download file from URL and convert to markdown using likhit.

    Pipeline: URL download -> temp file -> likhit/markitdown -> markdown.
    Returns None when conversion fails or produces insufficient content.
    """
    initial_hostname = urlparse(url).hostname
    if initial_hostname not in ALLOWED_HOSTS:
        logger.warning("Refusing to fetch untrusted host: %s", url)
        return None

    try:
        response = session.get(url, timeout=120, stream=True)
        response.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Failed to download %s: %s", url, exc)
        return None

    final_hostname = urlparse(response.url).hostname
    if final_hostname not in ALLOWED_HOSTS:
        logger.warning("Redirected to untrusted host: %s", response.url)
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
