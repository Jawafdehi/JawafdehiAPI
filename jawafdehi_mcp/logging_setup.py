"""Logging setup for jawafdehi-mcp — structlog + optional Sentry + optional GCP Cloud Logging."""

import logging
import os
import sys

import structlog
from structlog.types import EventDict, WrappedLogger

from jawafdehi_shared.logging_config import drop_transport_noise

SERVICE_NAME = "jawafdehi-mcp"


def _get_version() -> str:
    try:
        from . import __version__

        return __version__
    except ImportError:
        return "0.0.0"


def _sentry_processor(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Forward structlog context to Sentry scope (replaces removed StructlogIntegration)."""
    try:
        import sentry_sdk

        scope = sentry_sdk.get_current_scope()
        if scope:
            exclude = {
                "event",
                "level",
                "timestamp",
                "logger",
                "exception",
                "stack_info",
            }
            for key, value in event_dict.items():
                if key not in exclude:
                    scope.set_context(
                        "structlog", {**scope.contexts.get("structlog", {}), key: value}
                    )
    except Exception:  # noqa: BLE001 — enriching a log line must never break the log call
        pass
    return event_dict


def _init_sentry() -> None:
    # Opt-in error reporting: only initialize when a DSN is explicitly provided
    # (prod injects SENTRY_DSN via the jawafdehi-mcp-env secret). Local and
    # stdio-bridge dev runs leave it unset and stay silent — no events are
    # shipped from developer machines.
    sentry_dsn = os.getenv("SENTRY_DSN", "").strip()
    if not sentry_dsn:
        return

    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=sentry_dsn,
            environment=os.getenv("SENTRY_ENVIRONMENT", "production"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
            release=os.getenv("SENTRY_RELEASE", f"{SERVICE_NAME}@{_get_version()}"),
            before_send=drop_transport_noise,
        )
    except Exception:  # noqa: BLE001 — error reporting is optional; startup must survive it
        print("Failed to initialize Sentry SDK", file=sys.stderr)


def _init_gcp_logging() -> None:
    gcp_project = os.getenv("GCP_LOG_PROJECT", "").strip()
    if not gcp_project:
        return

    try:
        import google.cloud.logging

        client = google.cloud.logging.Client(project=gcp_project)
        client.setup_logging(
            log_level=_resolve_log_level(os.getenv("LOG_LEVEL", "INFO")),
        )
    except Exception:  # noqa: BLE001 — log shipping is optional; startup must survive it
        print("Failed to initialize GCP Cloud Logging", file=sys.stderr)


def _resolve_log_level(level_name: str) -> int:
    return getattr(logging, level_name.upper(), logging.INFO)


def setup_logging() -> None:
    """Configure structlog logging and optionally initialize Sentry."""
    _init_sentry()

    structlog.contextvars.bind_contextvars(service=SERVICE_NAME)

    timestamper = structlog.processors.TimeStamper(fmt="iso")

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        _sentry_processor,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        timestamper,
    ]

    debug = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")
    if debug:
        renderer = structlog.dev.ConsoleRenderer()
    else:
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=shared_processors
        + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(_resolve_log_level(os.getenv("LOG_LEVEL", "INFO")))

    _init_gcp_logging()
