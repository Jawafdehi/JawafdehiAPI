from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from django.core.cache import caches
from django.core.management.base import CommandError

MAX_CONVERSION_BYTES = 25 * 1024 * 1024
DOC_CONV_CACHE_ALIAS = "doc_conv"
DOC_CONV_CACHE_VERSION = "v1"


@dataclass(frozen=True)
class ConversionResult:
    markdown: str
    content_hash: str
    cache_key: str
    cache_hit: bool


def evidence_content_hash(content: bytes | str) -> str:
    if isinstance(content, str):
        content = content.encode("utf-8")
    return hashlib.sha256(content).hexdigest()


def idempotency_key(case_id: str, content_hash: str) -> str:
    return f"case:{case_id}:evidence:{content_hash}"


def doc_conv_cache_key(content_hash: str) -> str:
    return f"doc_conv:{DOC_CONV_CACHE_VERSION}:{content_hash}"


def convert_bytes_to_markdown(
    content: bytes,
    *,
    filename: str = "source.bin",
    cache_alias: str = DOC_CONV_CACHE_ALIAS,
    converter=None,
) -> ConversionResult:
    if len(content) > MAX_CONVERSION_BYTES:
        raise CommandError(
            f"Source is too large ({len(content)} bytes); max is {MAX_CONVERSION_BYTES} bytes."
        )

    digest = evidence_content_hash(content)
    cache_key = doc_conv_cache_key(digest)
    cache = caches[cache_alias]
    cached = cache.get(cache_key)
    if cached is not None:
        return ConversionResult(cached, digest, cache_key, True)

    markdown = _convert_uncached(content, filename=filename, converter=converter)
    cache.set(cache_key, markdown, timeout=None)
    return ConversionResult(markdown, digest, cache_key, False)


def convert_stream_to_markdown(
    stream: BinaryIO,
    *,
    filename: str = "source.bin",
    cache_alias: str = DOC_CONV_CACHE_ALIAS,
    converter=None,
) -> ConversionResult:
    content = stream.read(MAX_CONVERSION_BYTES + 1)
    return convert_bytes_to_markdown(
        content,
        filename=filename,
        cache_alias=cache_alias,
        converter=converter,
    )


def convert_path_to_markdown(
    path: Path | str,
    *,
    cache_alias: str = DOC_CONV_CACHE_ALIAS,
    converter=None,
) -> ConversionResult:
    path = Path(path)
    with path.open("rb") as f:
        return convert_stream_to_markdown(
            f,
            filename=path.name,
            cache_alias=cache_alias,
            converter=converter,
        )


def _convert_uncached(content: bytes, *, filename: str, converter=None) -> str:
    if converter is None:
        try:
            from markitdown import MarkItDown
        except ImportError as exc:
            raise CommandError(
                "markitdown is required for evidence conversion. "
                "Install conversion dependencies (markitdown + likhit plugin)."
            ) from exc
        converter = MarkItDown(enable_plugins=True)

    suffix = "".join(Path(filename).suffixes) or ".bin"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)
        result = converter.convert_uri(tmp_path.resolve().as_uri())
    finally:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)

    markdown = getattr(result, "markdown", None) or getattr(result, "text_content", "")
    return markdown or ""
