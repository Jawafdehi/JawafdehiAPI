import os

import structlog

SHARED_CONTEXT_VARS = [
    "request_id",
    "entity_id",
    "case_id",
    "revision_id",
    "service",
]

_IGNORED_TRANSPORT_ERROR_SIGNATURES = (
    "ClientDisconnect",
    "Attempted to exit cancel scope in a different task",
)


def add_service_name(_, __, event_dict):
    event_dict.setdefault("service", os.getenv("SERVICE_NAME", "jawafdehi-api"))
    return event_dict


def drop_transport_noise(event, hint):
    """Drop benign streamable-HTTP disconnect noise from Sentry."""
    exc_info = hint.get("exc_info") if hint else None
    if exc_info and exc_info[0] is not None:
        text = f"{exc_info[0].__name__}: {exc_info[1]}"
        if any(sig in text for sig in _IGNORED_TRANSPORT_ERROR_SIGNATURES):
            return None
    for value in (event.get("exception") or {}).get("values") or []:
        signature = f"{value.get('type', '')}: {value.get('value', '')}"
        if any(sig in signature for sig in _IGNORED_TRANSPORT_ERROR_SIGNATURES):
            return None
    return event


def configure_structlog():
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            add_service_name,
            # Django's LOGGING config owns the final renderer. Wrapping here
            # avoids rendering console text and then nesting it inside JSON.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
