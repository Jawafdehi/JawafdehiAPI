"""Shared utilities for enrichment management commands.

Consolidates SSRF-safe download, filename sanitization, and stream handling
that is common across enrich_* management commands.
"""

from __future__ import annotations

import hashlib
import http.client
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


def validate_host_safety(hostname: str | None, port: int = 0) -> list[tuple[str, int]]:
    """Resolve hostname once, reject internal IPs, return pinned addresses."""
    host = (hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("No hostname provided for download URL.")
    if host in _SSRF_BLOCKED_HOSTNAMES:
        raise ValueError(
            f"Blocked internal host: {hostname!r}. "
            "Download sources must target public hosts only."
        )
    try:
        addrinfo = socket.getaddrinfo(host, port)
    except socket.gaierror as exc:
        raise ValueError(
            f"Cannot resolve host: {hostname!r}. "
            "Only resolvable public hosts are allowed for source downloads."
        ) from exc

    pinned: list[tuple[str, int]] = []
    for info in addrinfo:
        addr = ipaddress.ip_address(info[4][0])
        if (
            addr.is_loopback
            or addr.is_private
            or addr.is_link_local
            or addr.is_reserved
        ):
            raise ValueError(
                f"Blocked internal address: {hostname!r} -> {addr}. "
                "Download sources must target public IPs only."
            )
        resolved_port = info[4][1] if len(info[4]) > 1 else port
        pinned.append((str(addr), resolved_port))
    return pinned


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """HTTPConnection that connects to a pre-resolved IP, pinning DNS."""

    _pinned_addr: str | None = None
    _pinned_port: int = 0

    def connect(self):
        if self._pinned_addr is None:
            return super().connect()
        self.sock = socket.create_connection(
            (self._pinned_addr, self._pinned_port or 80), self.timeout
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection that connects to a pre-resolved IP, pinning DNS."""

    _pinned_addr: str | None = None
    _pinned_port: int = 0

    def connect(self):
        if self._pinned_addr is None:
            return super().connect()
        sock = socket.create_connection(
            (self._pinned_addr, self._pinned_port or 443), self.timeout
        )
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class PinnedHTTPHandler(urllib.request.HTTPHandler):
    """HTTP handler that pins connections to pre-validated IP addresses."""

    def __init__(self, pinned_addrs: list[tuple[str, int]], host: str):
        super().__init__()
        self._pinned = pinned_addrs
        self._host = host

    def http_open(self, req):
        return self.do_open(self._make_connection, req)

    def _make_connection(self, host, timeout=None, **kwargs):
        conn = _PinnedHTTPConnection(host, timeout=timeout, **kwargs)
        if self._pinned:
            conn._pinned_addr, conn._pinned_port = self._pinned[0]
        return conn


class PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS handler that pins connections to pre-validated IP addresses."""

    def __init__(self, pinned_addrs: list[tuple[str, int]], host: str):
        super().__init__()
        self._pinned = pinned_addrs
        self._host = host

    def https_open(self, req):
        return self.do_open(self._make_connection, req)

    def _make_connection(self, host, timeout=None, **kwargs):
        conn = _PinnedHTTPSConnection(host, timeout=timeout, **kwargs)
        if self._pinned:
            conn._pinned_addr, conn._pinned_port = self._pinned[0]
        return conn


def build_pinned_opener(
    url: str, pinned_addrs: list[tuple[str, int]]
) -> urllib.request.OpenerDirector:
    """Build a urllib opener that pins DNS for *url* to *pinned_addrs*."""
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or ""

    if parsed.scheme == "https":
        handler = PinnedHTTPSHandler(pinned_addrs, host)
    else:
        handler = PinnedHTTPHandler(pinned_addrs, host)

    return urllib.request.build_opener(handler, SafeRedirectHandler(host))


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, host: str):
        self._host = host.lower().rstrip(".")

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        redirect_host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                f"Unsafe redirect scheme/host to {newurl}",
                headers,
                fp,
            )
        if redirect_host != self._host:
            raise urllib.error.HTTPError(
                req.full_url,
                code,
                f"Refusing cross-host redirect to {newurl}",
                headers,
                fp,
            )
        validate_host_safety(redirect_host, parsed.port or 0)
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
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
