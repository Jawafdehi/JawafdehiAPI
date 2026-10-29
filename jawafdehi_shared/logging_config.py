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

#: ``DisallowedHost`` is a client error, not a server fault: Django has already
#: rejected the request with a 400 before any of our code runs. Reporting it
#: makes the busiest issue in the project a log of other people's port scans —
#: 25,293 events on one issue, 77% of all event volume across the org, 10 out of
#: 10 sampled requests probing for leaked cloud credentials
#: (``/gcp-key.json``, ``/service-account.json``,
#: ``/.config/gcloud/application_default_credentials.json``, ``phpinfo.php``).
#:
#: Dropping it costs one signal: an ALLOWED_HOSTS misconfiguration after a
#: deploy would no longer page. That is covered better elsewhere and earlier —
#: `manage.py check --deploy` runs in CI, and a host that is genuinely ours but
#: missing from the allowlist fails its readiness probe rather than quietly
#: serving. Set ``SENTRY_REPORT_DISALLOWED_HOST=true`` to put it back while
#: investigating one.
_DISALLOWED_HOST_TYPE = "DisallowedHost"


def _report_disallowed_host():
    return os.getenv("SENTRY_REPORT_DISALLOWED_HOST", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def add_service_name(_, __, event_dict):
    event_dict.setdefault("service", os.getenv("SERVICE_NAME", "jawafdehi-api"))
    return event_dict


def _exception_types(event, hint):
    """Every exception class named by this event, from either source."""
    types = []
    exc_info = hint.get("exc_info") if hint else None
    if exc_info and exc_info[0] is not None:
        types.append(exc_info[0].__name__)
    for value in (event.get("exception") or {}).get("values") or []:
        if value.get("type"):
            types.append(value["type"])
    return types


def drop_transport_noise(event, hint):
    """Drop benign disconnect noise and unauthenticated host probes from Sentry."""
    exc_info = hint.get("exc_info") if hint else None
    if exc_info and exc_info[0] is not None:
        text = f"{exc_info[0].__name__}: {exc_info[1]}"
        if any(sig in text for sig in _IGNORED_TRANSPORT_ERROR_SIGNATURES):
            return None
    for value in (event.get("exception") or {}).get("values") or []:
        signature = f"{value.get('type', '')}: {value.get('value', '')}"
        if any(sig in signature for sig in _IGNORED_TRANSPORT_ERROR_SIGNATURES):
            return None
    if not _report_disallowed_host() and _DISALLOWED_HOST_TYPE in _exception_types(
        event, hint
    ):
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
