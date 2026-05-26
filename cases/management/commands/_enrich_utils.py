"""Shared utilities for enrichment management commands.

Consolidates SSRF-safe download, filename sanitization, and stream handling
that is common across enrich_* management commands.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from django.core.management.base import CommandError

MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024
DOWNLOAD_CHUNK_SIZE = 16 * 1024

# SSRF protection: block well-known internal/metadata endpoints.
_CLOUD_METADATA_IP = "169.254.169.254"  # NOSONAR — link-local, not configurable

_SSRF_BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "metadata.google.internal",
        _CLOUD_METADATA_IP,
        "metadata",
        "0.0.0.0",
    }
)


def validate_host_safety(hostname: str | None) -> None:
    host = (hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("No hostname provided for download URL.")
    if host in _SSRF_BLOCKED_HOSTNAMES:
        raise ValueError(
            f"Blocked internal host: {hostname!r}. "
            "Download sources must target public hosts only."
        )
    try:
        addrinfo = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(
            f"Cannot resolve host: {hostname!r}. "
            "Only resolvable public hosts are allowed for source downloads."
        ) from exc
    for info in addrinfo:
        addr = ipaddress.ip_address(info[4][0])
        if (
            addr.is_loopback
            or addr.is_private
            or addr.is_link_local
            or addr.is_reserved
        ):
            raise ValueError(
                f"Blocked internal address: {hostname!r} → {addr}. "
                "Download sources must target public IPs only."
            )


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                f"Unsafe redirect scheme/host to {newurl}",
                headers,
                fp,
            )
        validate_host_safety(parsed.hostname)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def sanitize_download_filename(filename: str | None, source_id: str) -> str:
    raw = (filename or "").strip()
    if not raw:
        return f"{source_id}.bin"

    decoded = urllib.parse.unquote(raw)
    candidate = Path(decoded).name.strip()

    if candidate in {"", ".", ".."}:
        return f"{source_id}.bin"

    candidate = candidate.replace("\x00", "")
    candidate = re.sub(r"[<>:\"/\\|?*]+", "_", candidate).rstrip(" .")
    if candidate in {"", ".", ".."}:
        return f"{source_id}.bin"

    max_len = 200
    if len(candidate) <= max_len:
        return candidate

    suffix = "".join(Path(candidate).suffixes)
    stem = candidate[: -len(suffix)] if suffix else candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:10]

    stem_budget = max_len - len(suffix) - len(digest) - 1
    if stem_budget < 1:
        return f"{source_id}-{digest}{suffix}"[:max_len]

    truncated_stem = stem[:stem_budget].rstrip(" .-_")
    if not truncated_stem:
        truncated_stem = source_id

    return f"{truncated_stem}-{digest}{suffix}"


def confined_output_path(output_dir: Path, filename: str) -> Path:
    output_dir_resolved = output_dir.resolve()
    out_path = (output_dir / filename).resolve()
    if output_dir_resolved not in out_path.parents:
        raise CommandError(f"Refusing to write outside output directory: '{filename}'")
    return out_path


def copy_stream_to_path_with_limit(in_file: Any, out_path: Path) -> None:
    total_bytes = 0
    try:
        with out_path.open("wb") as out_file:
            while True:
                chunk = in_file.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > MAX_DOWNLOAD_BYTES:
                    raise CommandError(
                        f"Downloaded source exceeds max size of {MAX_DOWNLOAD_BYTES} bytes."
                    )
                out_file.write(chunk)
    except OSError:
        out_path.unlink(missing_ok=True)
        raise
    except CommandError:
        out_path.unlink(missing_ok=True)
        raise
