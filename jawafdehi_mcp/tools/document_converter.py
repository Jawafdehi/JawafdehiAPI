"""Unified document conversion with bounded local and remote inputs."""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import ipaddress
import math
import multiprocessing
import os
import socket
import threading
import weakref
from dataclasses import dataclass
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any, Callable, NoReturn
from urllib.parse import unquote, unquote_to_bytes, urljoin, urlparse
from urllib.request import url2pathname

import httpcore
import httpx
import structlog
from markitdown import MarkItDown, StreamInfo
from mcp.types import TextContent

from ..request_context import is_local_stdio_transport
from .base import BaseTool, error_text

logger = structlog.get_logger()

_REMOTE_SCHEMES = frozenset({"http", "https"})
_ALLOWED_REMOTE_PORTS = frozenset({80, 443})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 5
_USER_AGENT = "jawafdehi-mcp/1.0"

_conversion_limiter_lock = threading.Lock()
_conversion_limiters: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, tuple[int, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def _positive_int_env(name: str, default: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    return min(value, maximum)


def _positive_float_env(name: str, default: float, maximum: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    return min(value, maximum)


def _max_input_bytes() -> int:
    return _positive_int_env(
        "MCP_DOCUMENT_MAX_INPUT_BYTES",
        25 * 1024 * 1024,
        100 * 1024 * 1024,
    )


def _max_output_chars() -> int:
    return _positive_int_env(
        "MCP_DOCUMENT_MAX_OUTPUT_CHARS",
        5_000_000,
        20_000_000,
    )


def _fetch_timeout_seconds() -> float:
    return _positive_float_env("MCP_DOCUMENT_FETCH_TIMEOUT", 20.0, 120.0)


def _conversion_timeout_seconds() -> float:
    return _positive_float_env("MCP_DOCUMENT_CONVERT_TIMEOUT", 120.0, 600.0)


def _conversion_max_concurrency() -> int:
    return _positive_int_env("MCP_DOCUMENT_MAX_CONCURRENCY", 2, 16)


def _worker_memory_bytes() -> int:
    return _positive_int_env(
        "MCP_DOCUMENT_WORKER_MEMORY_BYTES",
        1024 * 1024 * 1024,
        2 * 1024 * 1024 * 1024,
    )


def _conversion_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    limit = _conversion_max_concurrency()
    with _conversion_limiter_lock:
        existing = _conversion_limiters.get(loop)
        if existing is None or existing[0] != limit:
            existing = (limit, asyncio.Semaphore(limit))
            _conversion_limiters[loop] = existing
        return existing[1]


def _is_global_address(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except ValueError:
        return False


def _normalized_hostname(value: str) -> str:
    host = value.rstrip(".")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return host.encode("idna").decode("ascii").lower()


@dataclass(frozen=True, slots=True)
class _ResolvedRemoteTarget:
    hostname: str
    port: int
    addresses: tuple[str, ...]


async def _validate_remote_url(uri: str) -> _ResolvedRemoteTarget:
    """Validate and resolve one URL hop, returning only approved peer addresses."""
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()
    if scheme not in _REMOTE_SCHEMES:
        raise ValueError("Remote documents must use http:// or https://.")
    if parsed.username or parsed.password:
        raise ValueError("Remote document URLs must not contain credentials.")
    if not parsed.hostname:
        raise ValueError("Remote document URL must include a hostname.")
    if "%" in parsed.hostname:
        raise ValueError("IPv6 zone identifiers are not allowed in remote URLs.")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("Remote document URL contains an invalid port.") from exc
    if port not in _ALLOWED_REMOTE_PORTS:
        raise ValueError("Remote document URLs may use only ports 80 and 443.")

    host = parsed.hostname.rstrip(".")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            async with asyncio.timeout(3.0):
                records = await asyncio.to_thread(
                    socket.getaddrinfo,
                    host,
                    port,
                    type=socket.SOCK_STREAM,
                )
        except TimeoutError as exc:
            raise ValueError("Remote document hostname lookup timed out.") from exc
        except socket.gaierror as exc:
            raise ValueError("Remote document hostname could not be resolved.") from exc
        addresses = tuple(dict.fromkeys(record[4][0] for record in records))
        if not addresses or any(not _is_global_address(value) for value in addresses):
            raise ValueError(
                "Remote document URLs must resolve only to public IP addresses."
            )
    else:
        if not literal.is_global:
            raise ValueError("Remote document URLs must target a public IP address.")
        addresses = (literal.compressed,)

    return _ResolvedRemoteTarget(
        hostname=_normalized_hostname(host),
        port=port,
        addresses=addresses,
    )


class _PinnedNetworkBackend:
    """Connect to prevalidated IPs while preserving the URL host for TLS SNI."""

    def __init__(self, backend: Any, target: _ResolvedRemoteTarget) -> None:
        self._backend = backend
        self._target = target

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> Any:
        if (
            _normalized_hostname(host) != self._target.hostname
            or port != self._target.port
        ):
            raise httpcore.ConnectError("Remote document connection target changed.")

        last_error: Exception | None = None
        for address in self._target.addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:  # noqa: BLE001 — try the next prevalidated address
                last_error = exc
        if last_error is not None:
            raise last_error
        raise httpcore.ConnectError("Remote document hostname has no approved address.")

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Any = None,
    ) -> NoReturn:
        raise httpcore.ConnectError("Unix sockets are not valid remote targets.")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedHTTPTransport(httpx.AsyncHTTPTransport):
    """HTTPX transport whose socket backend cannot perform a second DNS lookup."""

    def __init__(self, target: _ResolvedRemoteTarget) -> None:
        super().__init__(
            trust_env=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        )
        # HTTPX does not expose custom DNS resolution publicly. Wrapping
        # httpcore's backend keeps the original URL host for Host/TLS SNI while
        # making the actual TCP peer one of the addresses validated above.
        backend = self._pool._network_backend
        self._pool._network_backend = _PinnedNetworkBackend(backend, target)


def _stream_info_for_response(response: httpx.Response) -> StreamInfo:
    content_type = response.headers.get("content-type", "")
    parts = [part.strip() for part in content_type.split(";") if part.strip()]
    mimetype = parts[0] if parts else None
    charset = None
    for part in parts[1:]:
        if part.lower().startswith("charset="):
            charset = part.split("=", 1)[1].strip("\"'")
            break

    path = unquote(response.url.path)
    filename = Path(path).name or None
    extension = Path(filename).suffix if filename else None
    return StreamInfo(
        mimetype=mimetype,
        charset=charset,
        filename=filename,
        extension=extension,
        url=str(response.url),
    )


async def _fetch_remote_document(uri: str) -> tuple[bytes, StreamInfo]:
    max_bytes = _max_input_bytes()
    timeout = _fetch_timeout_seconds()
    current = uri

    async with asyncio.timeout(timeout):
        for redirect_count in range(_MAX_REDIRECTS + 1):
            target = await _validate_remote_url(current)
            async with httpx.AsyncClient(
                transport=_PinnedHTTPTransport(target),
                follow_redirects=False,
                timeout=httpx.Timeout(
                    connect=min(5.0, timeout),
                    read=min(10.0, timeout),
                    write=min(10.0, timeout),
                    pool=min(5.0, timeout),
                ),
                trust_env=False,
                headers={"User-Agent": _USER_AGENT},
            ) as client:
                async with client.stream("GET", current) as response:
                    if response.status_code in _REDIRECT_STATUSES:
                        if redirect_count == _MAX_REDIRECTS:
                            raise ValueError(
                                f"Remote document exceeded {_MAX_REDIRECTS} redirects."
                            )
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError(
                                "Remote document redirect omitted Location."
                            )
                        current = urljoin(str(response.url), location)
                        continue

                    response.raise_for_status()
                    content_length = response.headers.get("content-length")
                    if content_length:
                        try:
                            declared = int(content_length)
                        except ValueError:
                            declared = 0
                        if declared > max_bytes:
                            raise ValueError(
                                f"Remote document exceeds the {max_bytes}-byte limit."
                            )

                    payload = bytearray()
                    async for chunk in response.aiter_bytes():
                        payload.extend(chunk)
                        if len(payload) > max_bytes:
                            raise ValueError(
                                f"Remote document exceeds the {max_bytes}-byte limit."
                            )
                    return bytes(payload), _stream_info_for_response(response)

    raise ValueError("Remote document fetch did not complete.")


def _decode_data_uri(uri: str) -> tuple[bytes, StreamInfo]:
    try:
        header, encoded = uri[5:].split(",", 1)
    except ValueError as exc:
        raise ValueError("Malformed data URI.") from exc

    max_bytes = _max_input_bytes()
    if len(encoded) > max_bytes * 4 + 16:
        raise ValueError(f"Data URI exceeds the {max_bytes}-byte limit.")

    parts = header.split(";") if header else []
    mimetype = parts[0] if parts and "/" in parts[0] else "text/plain"
    attributes = parts[1:] if parts and "/" in parts[0] else parts
    is_base64 = bool(attributes and attributes[-1].lower() == "base64")
    if is_base64:
        attributes = attributes[:-1]
        try:
            payload = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("Malformed base64 data URI.") from exc
    else:
        payload = unquote_to_bytes(encoded)

    if len(payload) > max_bytes:
        raise ValueError(f"Data URI exceeds the {max_bytes}-byte limit.")

    charset = None
    for attribute in attributes:
        if attribute.lower().startswith("charset="):
            charset = attribute.split("=", 1)[1].strip("\"'")
            break
    return payload, StreamInfo(mimetype=mimetype, charset=charset)


async def _run_bounded_conversion(
    operation: Callable[[], Any],
    acquired_limiter: asyncio.Semaphore,
) -> Any:
    """Run a trusted local-file parser without blocking the ASGI event loop.

    HTTP callers cannot reach this path. Untrusted remote and inline documents
    use the killable subprocess path below.
    """
    worker: asyncio.Task | None = None
    try:
        async with asyncio.timeout(_conversion_timeout_seconds()):
            worker = asyncio.create_task(asyncio.to_thread(operation))
            return await asyncio.shield(worker)
    except TimeoutError as exc:
        raise ValueError("Document conversion timed out.") from exc
    finally:
        if worker is None or worker.done():
            acquired_limiter.release()
        else:
            # A Python worker thread cannot be killed safely. Retain its
            # capacity slot until it exits so repeated timeouts cannot grow
            # an unbounded pool of still-running parsers.
            worker.add_done_callback(
                lambda task: _consume_conversion_task(task, acquired_limiter)
            )


def _consume_conversion_task(
    task: asyncio.Task,
    acquired_limiter: asyncio.Semaphore,
) -> None:
    """Release retained capacity and consume a detached parser outcome."""
    acquired_limiter.release()
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:  # noqa: BLE001 — retrieving it IS the point; see below
        # The caller already received a timeout. Retrieving the late exception
        # prevents asyncio from reporting an unobserved detached-task failure.
        pass


@dataclass(frozen=True, slots=True)
class _StreamConversionRequest:
    payload: bytes
    stream_info: dict[str, str | None]
    kwargs: dict[str, Any]
    max_output_chars: int


def _apply_worker_resource_limits() -> None:
    """Apply best-effort POSIX limits before an untrusted parser runs."""
    try:
        import resource
    except ImportError:  # pragma: no cover - production image is Linux
        return

    cpu_seconds = max(1, math.ceil(_conversion_timeout_seconds()))
    file_bytes = max(_max_input_bytes() * 4, _max_output_chars() * 2)
    limits = (
        (resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 1),
        (resource.RLIMIT_FSIZE, file_bytes, file_bytes),
        (resource.RLIMIT_NOFILE, 64, 64),
        (resource.RLIMIT_CORE, 0, 0),
    )
    for kind, soft, hard in limits:
        try:
            resource.setrlimit(kind, (soft, hard))
        except (OSError, ValueError):
            # Containers can impose a stricter immutable hard limit. Retain it
            # rather than failing conversion before the parser starts.
            continue


def _process_rss_bytes(process: multiprocessing.Process) -> int | None:
    """Return a Linux child process's resident memory, if observable."""
    if process.pid is None:
        return None
    try:
        status = Path(f"/proc/{process.pid}/status").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None
    for line in status.splitlines():
        if line.startswith("VmRSS:"):
            try:
                return int(line.split()[1]) * 1024
            except (IndexError, ValueError):
                return None
    return None


def _stream_conversion_worker(
    send_connection: Connection,
    request: _StreamConversionRequest,
) -> None:
    """Child-process entrypoint for untrusted stream conversion."""
    try:
        _apply_worker_resource_limits()
        result = MarkItDown(enable_plugins=False).convert_stream(
            io.BytesIO(request.payload),
            stream_info=StreamInfo(**request.stream_info),
            **request.kwargs,
        )
        markdown = result.markdown
        if not isinstance(markdown, str):
            raise ValueError("Document converter returned an invalid result.")
        if len(markdown) > request.max_output_chars:
            raise ValueError("Converted Markdown exceeds the configured output limit.")
        # Never send pickled objects from the untrusted worker. If a parser were
        # compromised, Connection.recv() would otherwise give it a pickle-based
        # code-execution path back into the parent process.
        send_connection.send_bytes(b"\x00" + markdown.encode("utf-8"))
    # noqa justified: this is the child process's outer boundary. The parent
    # reads the reason off the pipe, so ANY exit has to be reported there —
    # including the SystemExit/MemoryError that _apply_worker_resource_limits'
    # rlimits raise. Letting one escape gives the parent a silent EOF instead.
    except BaseException as exc:  # noqa: BLE001
        try:
            error = f"{type(exc).__name__}: {exc}".encode("utf-8", errors="replace")[
                :8192
            ]
            send_connection.send_bytes(b"\x01" + error)
        except Exception:  # noqa: BLE001 — a broken pipe here leaves the parent its EOF path
            pass
    finally:
        send_connection.close()


def _stop_process(process: multiprocessing.Process) -> None:
    if not process.is_alive():
        process.join(timeout=0.1)
        return
    process.terminate()
    process.join(timeout=1.0)
    if process.is_alive():
        process.kill()
        process.join(timeout=1.0)


async def _run_isolated_stream_conversion(
    payload: bytes,
    stream_info: StreamInfo,
    kwargs: dict[str, Any],
) -> str:
    """Convert untrusted bytes in a killable, resource-limited subprocess."""
    context = multiprocessing.get_context("spawn")
    receive_connection, send_connection = context.Pipe(duplex=False)
    request = _StreamConversionRequest(
        payload=payload,
        stream_info={
            "mimetype": stream_info.mimetype,
            "extension": stream_info.extension,
            "charset": stream_info.charset,
            "filename": stream_info.filename,
            "local_path": stream_info.local_path,
            "url": stream_info.url,
        },
        kwargs=kwargs,
        max_output_chars=_max_output_chars(),
    )
    process = context.Process(
        target=_stream_conversion_worker,
        args=(send_connection, request),
        daemon=True,
        name="mcp-document-converter",
    )
    timeout = _conversion_timeout_seconds()
    deadline = asyncio.get_running_loop().time() + timeout
    started = False
    try:
        process.start()
        started = True
        send_connection.close()

        # Drain the pipe before waiting for process exit. A Markdown result can
        # be much larger than the OS pipe buffer; waiting for exit first would
        # deadlock the child in send_bytes() until the conversion timeout.
        while not receive_connection.poll():
            if not process.is_alive():
                raise ValueError(
                    "Document conversion worker exited unexpectedly "
                    f"(status {process.exitcode})."
                )
            rss_bytes = _process_rss_bytes(process)
            if rss_bytes is not None and rss_bytes > _worker_memory_bytes():
                raise ValueError(
                    "Document conversion exceeded the configured memory limit."
                )
            if asyncio.get_running_loop().time() >= deadline:
                raise ValueError("Document conversion timed out.")
            await asyncio.sleep(0.05)

        remaining = max(0.01, deadline - asyncio.get_running_loop().time())
        max_message_bytes = _max_output_chars() * 4 + 8193
        try:
            async with asyncio.timeout(remaining):
                message = await asyncio.to_thread(
                    receive_connection.recv_bytes,
                    max_message_bytes,
                )
        except TimeoutError as exc:
            raise ValueError("Document conversion timed out.") from exc
        except (EOFError, OSError) as exc:
            raise ValueError(
                "Document conversion worker returned an invalid result."
            ) from exc

        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        await asyncio.to_thread(process.join, min(1.0, remaining))
        if process.is_alive():
            raise ValueError("Document conversion worker failed to exit.")
        if process.exitcode != 0:
            raise ValueError(
                "Document conversion worker exited unexpectedly "
                f"(status {process.exitcode})."
            )
        if not message:
            raise ValueError("Document conversion worker returned an invalid result.")
        value = message[1:].decode("utf-8", errors="replace")
        if message[0] == 0:
            return value
        if message[0] == 1:
            raise ValueError(f"Document conversion failed: {value}")
        raise ValueError("Document conversion worker returned an invalid result.")
    finally:
        receive_connection.close()
        send_connection.close()
        if started:
            await asyncio.to_thread(_stop_process, process)
            process.close()


async def _acquire_document_slot() -> asyncio.Semaphore:
    limiter = _conversion_limiter()
    try:
        async with asyncio.timeout(_conversion_timeout_seconds()):
            await limiter.acquire()
    except TimeoutError as exc:
        raise ValueError("Document conversion capacity is exhausted.") from exc
    return limiter


class DocumentConverterTool(BaseTool):
    """Convert a bounded local, remote, or inline document to Markdown."""

    @property
    def name(self) -> str:
        return "convert_to_markdown"

    @property
    def description(self) -> str:
        return (
            "Convert documents to Markdown through MarkItDown. Local file_path "
            "and file:// inputs, plus output_path, are available only through "
            "local stdio. HTTP(S) downloads and data URIs are size- and "
            "time-bounded and parsed in a resource-limited subprocess with "
            "plugins disabled. Installed plugins such as `likhit` are opt-in "
            "for trusted local stdio files only. Optional PDF pages use a "
            "1-based page or page range."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": (
                        "Absolute local path. Available only through local stdio "
                        "and mutually exclusive with uri."
                    ),
                },
                "uri": {
                    "type": "string",
                    "description": (
                        "Explicit file:// (local stdio only), http://, https://, "
                        "or data: URI. Bare filesystem paths are rejected."
                    ),
                },
                "output_path": {
                    "type": "string",
                    "description": (
                        "Optional local Markdown destination, supported only "
                        "through local stdio."
                    ),
                },
                "pages": {
                    "type": "string",
                    "pattern": r"^[1-9]\d*(?:-[1-9]\d*)?$",
                    "description": "Optional 1-based PDF page or inclusive range.",
                },
                "enable_plugins": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Enable installed plugins for trusted local stdio file "
                        "inputs. Remote and data URI conversion never loads "
                        "plugins."
                    ),
                },
            },
            "required": [],
            "oneOf": [
                {"required": ["file_path"]},
                {"required": ["uri"]},
            ],
        }

    def _get_source_path(self, arguments: dict[str, Any]) -> tuple[str, bool]:
        file_path = arguments.get("file_path")
        uri = arguments.get("uri")
        if file_path and uri:
            raise ValueError(
                "Cannot specify both 'file_path' and 'uri'. Use one or the other."
            )
        if file_path:
            return str(file_path), True
        if not uri:
            raise ValueError("Must specify either 'file_path' or 'uri'.")
        if not isinstance(uri, str):
            raise ValueError("'uri' must be a string.")

        parsed = urlparse(uri)
        scheme = parsed.scheme.lower()
        if scheme == "file":
            if parsed.netloc not in ("", "localhost"):
                raise ValueError(
                    "Unsupported file URI. Netloc must be empty or localhost."
                )
            return url2pathname(unquote(parsed.path)), True
        if scheme in _REMOTE_SCHEMES or scheme == "data":
            return uri, False
        if not scheme:
            raise ValueError(
                "Bare paths are not valid URIs; use file_path through local stdio."
            )
        raise ValueError(
            "Unsupported URI scheme. Use file://, http://, https://, or data:."
        )

    @staticmethod
    def _get_output_path(arguments: dict[str, Any]) -> Path | None:
        output_path = arguments.get("output_path")
        return Path(output_path) if output_path else None

    @staticmethod
    def _is_local_stdio() -> bool:
        return is_local_stdio_transport()

    @staticmethod
    def _conversion_kwargs(arguments: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        pages = arguments.get("pages")
        if pages:
            if not isinstance(pages, str):
                raise ValueError("'pages' must be a string.")
            if "-" in pages:
                start_page, end_page = map(int, pages.split("-", 1))
                if start_page > end_page:
                    raise ValueError("Invalid 'pages' range: start must be <= end.")
            kwargs["pages"] = pages
        return kwargs

    async def _convert_with_markitdown(
        self,
        source: str,
        *,
        is_local_file: bool,
        arguments: dict[str, Any],
    ) -> str:
        kwargs = self._conversion_kwargs(arguments)
        enable_plugins = bool(arguments.get("enable_plugins", False))
        limiter = await _acquire_document_slot()

        if is_local_file:
            source_uri = Path(source).resolve().as_uri()

            def convert() -> Any:
                return MarkItDown(enable_plugins=enable_plugins).convert_uri(
                    source_uri, **kwargs
                )

            conversion_started = False
            try:
                conversion_started = True
                result = await _run_bounded_conversion(convert, limiter)
            finally:
                if not conversion_started:
                    limiter.release()
            markdown = result.markdown
        else:
            try:
                if enable_plugins:
                    raise ValueError(
                        "enable_plugins is supported only for trusted local "
                        "stdio file inputs."
                    )
                if source.lower().startswith(("http://", "https://")):
                    payload, stream_info = await _fetch_remote_document(source)
                else:
                    payload, stream_info = _decode_data_uri(source)
                markdown = await _run_isolated_stream_conversion(
                    payload,
                    stream_info,
                    kwargs,
                )
            finally:
                limiter.release()

        if not isinstance(markdown, str):
            raise ValueError("Document converter returned an invalid result.")
        if len(markdown) > _max_output_chars() and not arguments.get("output_path"):
            raise ValueError(
                "Converted Markdown is too large to return; use output_path "
                "through local stdio."
            )
        return markdown

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        output_path = self._get_output_path(arguments)
        if output_path and not self._is_local_stdio():
            return error_text(
                "Error: output_path is only supported by local stdio MCP "
                "servers. HTTP-hosted MCP servers must not write files."
            )

        try:
            source, is_local_file = self._get_source_path(arguments)
        except ValueError as exc:
            return error_text(f"Error: {exc}")

        if is_local_file and not self._is_local_stdio():
            return error_text(
                "Error: local file inputs are only supported by local stdio "
                "MCP servers. HTTP-hosted MCP servers cannot read server "
                "filesystem paths."
            )

        if is_local_file:
            path = Path(source)
            if not path.exists():
                return error_text(f"Error: File not found: {source}")
            if not path.is_file():
                return error_text(f"Error: Path is not a file: {source}")

        converter_used = "MarkItDown"
        if is_local_file and arguments.get("enable_plugins", False):
            converter_used += " + plugins"
        try:
            markdown = await self._convert_with_markitdown(
                source,
                is_local_file=is_local_file,
                arguments=arguments,
            )
        except Exception as exc:  # noqa: BLE001 — tool boundary: a bad document is a result, not a crash
            logger.warning(
                "document_conversion_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
            return error_text(f"Error converting document with {converter_used}: {exc}")

        if output_path:
            try:
                if is_local_file:
                    source_path = Path(source).resolve(strict=False)
                    target_path = output_path.resolve(strict=False)
                    if target_path == source_path:
                        return error_text(
                            "Error: output_path must differ from the source file."
                        )
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(markdown, encoding="utf-8")
                return [
                    TextContent(
                        type="text",
                        text=(
                            f"Converted with {converter_used}\n"
                            f"Markdown written to {output_path}"
                        ),
                    )
                ]
            except Exception as exc:  # noqa: BLE001 — tool boundary: report the write failure to the caller
                return error_text(f"Error writing to {output_path}: {exc}")

        return [
            TextContent(
                type="text",
                text=f"Converted with {converter_used}\n\n{markdown}",
            )
        ]
