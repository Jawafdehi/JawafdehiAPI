import os
import time

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
#: rejected the request with a 400 before any of our code runs. It was also the
#: busiest issue in the org — 25,293 events on one issue, 77% of all event
#: volume, 10 of 10 sampled requests probing for leaked cloud credentials
#: (``/gcp-key.json``, ``/service-account.json``,
#: ``/.config/gcloud/application_default_credentials.json``, ``phpinfo.php``).
#:
#: RATE-LIMITED, NOT DROPPED. An earlier version dropped it outright and
#: justified that with two compensating controls that do not exist in this
#: repo: ``manage.py check --deploy`` is nowhere in ``.github/``, and the
#: readiness probe cannot catch a bad ``ALLOWED_HOSTS`` because
#: ``config/settings.py`` appends ``POD_IP`` to it — the kubelet probes by pod
#: IP, so it passes while every public request 400s.
#:
#: What makes rate-limiting sufficient rather than merely softer: Django logs
#: EVERY occurrence through ``django.security.DisallowedHost``, independently
#: of Sentry, and that lands in VictoriaLogs with 90d retention (verified:
#: 275 records in 24h). So the per-occurrence signal is not Sentry's to carry.
#: Sentry only needs a low-rate heartbeat that the condition exists.
#:
#: The window has to be sized against processes, not pods — the Dockerfile runs
#: ``--workers 2`` and the limiter is process-local, so the ceiling is
#: ``replicas x 2 / window``. At one hour that ceiling sits ABOVE the observed
#: ~160 events/day and the limiter would almost never bind; a day brings it to
#: single digits. An ongoing misconfiguration still re-reports daily rather
#: than going silent, and shows up immediately in the logs and in the 400 rate
#: at the proxy.
_DISALLOWED_HOST_TYPE = "DisallowedHost"
_DISALLOWED_HOST_WINDOW_ENV = "SENTRY_DISALLOWED_HOST_WINDOW_SECONDS"
_DEFAULT_DISALLOWED_HOST_WINDOW = 86400.0

#: Monotonic timestamp of the last DisallowedHost event allowed through, per
#: process. Deliberately process-local and lock-free: ``before_send`` runs on
#: the reporting thread, the value is a single float, and the worst case from a
#: race is one extra event — which is cheaper than serialising every event in
#: every worker behind a lock.
_last_disallowed_host_report = [float("-inf")]


def _disallowed_host_window():
    try:
        window = float(
            os.getenv(_DISALLOWED_HOST_WINDOW_ENV, _DEFAULT_DISALLOWED_HOST_WINDOW)
        )
    except (TypeError, ValueError):
        return _DEFAULT_DISALLOWED_HOST_WINDOW
    return max(0.0, window)


def _allow_disallowed_host_report(now=None):
    """Whether this DisallowedHost event is the one to report this window."""
    window = _disallowed_host_window()
    if window <= 0:
        return True  # rate limiting switched off
    current = time.monotonic() if now is None else now
    if current - _last_disallowed_host_report[0] < window:
        return False
    _last_disallowed_host_report[0] = current
    return True


def add_service_name(_, __, event_dict):
    event_dict.setdefault("service", os.getenv("SERVICE_NAME", "jawafdehi-api"))
    return event_dict


def _exception_signatures(event, hint):
    """``(type, value)`` for every exception this event names, oldest first.

    One traversal with its None-guards in one place: the same
    ``(event.get("exception") or {}).get("values") or []`` expression was
    written out three times in this module.
    """
    signatures = []
    exc_info = hint.get("exc_info") if hint else None
    if exc_info and exc_info[0] is not None:
        signatures.append((exc_info[0].__name__, str(exc_info[1])))
    for value in (event.get("exception") or {}).get("values") or []:
        signatures.append((value.get("type") or "", value.get("value") or ""))
    return signatures


def drop_transport_noise(event, hint):
    """Drop benign disconnect noise and rate-limit unauthenticated host probes."""
    signatures = _exception_signatures(event, hint)

    for exc_type, exc_value in signatures:
        text = f"{exc_type}: {exc_value}"
        if any(sig in text for sig in _IGNORED_TRANSPORT_ERROR_SIGNATURES):
            return None

    # Only the RAISED exception counts, not its causes. Sentry serialises a
    # `raise ... from exc` chain into `values` oldest-first, so matching
    # anywhere in the list would silently swallow an unrelated error that
    # merely happens to wrap a DisallowedHost.
    if signatures and signatures[-1][0] == _DISALLOWED_HOST_TYPE:
        if not _allow_disallowed_host_report():
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
