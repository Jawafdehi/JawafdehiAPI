import os
import re
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

#: ``{rejected host: monotonic timestamp of its last reported event}``.
#:
#: Keyed BY HOST, which is the whole point. A single global slot would be spent
#: by whichever DisallowedHost arrived first in the window — and with a scanner
#: contributing ~160 events a day against one genuine misconfiguration, that is
#: essentially never the one worth seeing. The real event would be dropped for
#: a day, and the heartbeat that did get through would name the scanner's host.
#: Per-host, a hostname nobody has seen before always reports immediately.
#:
#: Bounded so a scanner rotating Host headers cannot grow it without limit;
#: when full it is cleared rather than evicted one-by-one, which costs at most
#: one extra event per host and keeps this allocation-free on the hot path.
#:
#: Process-local and lock-free: ``before_send`` runs on the reporting thread,
#: and the worst case from a race is one duplicate event — cheaper than
#: serialising every event in every worker behind a lock.
_MAX_TRACKED_HOSTS = 512
_disallowed_host_last_reported: dict[str, float] = {}

#: ``Invalid HTTP_HOST header: 'evil.example'. You may need to add ...``
_DISALLOWED_HOST_PATTERN = re.compile(r"Invalid HTTP_HOST header: '([^']*)'")


def _disallowed_host_window():
    try:
        window = float(
            os.getenv(_DISALLOWED_HOST_WINDOW_ENV, _DEFAULT_DISALLOWED_HOST_WINDOW)
        )
    except (TypeError, ValueError):
        return _DEFAULT_DISALLOWED_HOST_WINDOW
    return max(0.0, window)


def _rejected_host(exc_value):
    """The host Django rejected, or "" when the message does not carry one."""
    match = _DISALLOWED_HOST_PATTERN.search(exc_value or "")
    return match.group(1) if match else ""


def _allow_disallowed_host_report(host, now=None):
    """Whether this host's DisallowedHost is the one to report this window."""
    window = _disallowed_host_window()
    if window <= 0:
        return True  # rate limiting switched off
    current = time.monotonic() if now is None else now
    last = _disallowed_host_last_reported.get(host)
    if last is not None and current - last < window:
        return False
    if len(_disallowed_host_last_reported) >= _MAX_TRACKED_HOSTS:
        # A scanner rotating Host headers must not grow this without limit.
        _disallowed_host_last_reported.clear()
    _disallowed_host_last_reported[host] = current
    return True


def add_service_name(_, __, event_dict):
    event_dict.setdefault("service", os.getenv("SERVICE_NAME", "jawafdehi-api"))
    return event_dict


def _exception_signatures(event, hint):
    """``(type, value)`` for every exception this event names.

    NOT simply oldest-first: when ``hint["exc_info"]`` is present — the normal
    shape for the Django logging integration — the RAISED exception is
    prepended, so the list reads ``[raised, cause, ..., raised]``. What callers
    may rely on is the tail: ``signatures[-1]`` is always the raised exception,
    because Sentry serialises ``values`` oldest-cause-first. The DisallowedHost
    branch depends on exactly that, so do not reorder this without revisiting
    it, and do not read ``signatures[0]`` expecting a root cause.

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

    # NOTE the asymmetry with the DisallowedHost branch below, which matches
    # only the raised exception. This one matches anywhere in the chain, and
    # that is left as it was found rather than quietly tightened: a disconnect
    # genuinely does arrive as a cause here, wrapped by anyio/starlette task
    # groups, so restricting it to the raised exception would reintroduce the
    # noise these signatures were added to suppress. The cost is real though —
    # an unrelated error whose __context__ happens to be a ClientDisconnect is
    # dropped — and narrowing it wants its own change, with the disconnect
    # shapes measured first.
    for exc_type, exc_value in signatures:
        text = f"{exc_type}: {exc_value}"
        if any(sig in text for sig in _IGNORED_TRANSPORT_ERROR_SIGNATURES):
            return None

    # Only the RAISED exception counts, not its causes. Sentry serialises a
    # `raise ... from exc` chain into `values` oldest-first, so matching
    # anywhere in the list would silently swallow an unrelated error that
    # merely happens to wrap a DisallowedHost.
    if signatures and signatures[-1][0] == _DISALLOWED_HOST_TYPE:
        if not _allow_disallowed_host_report(_rejected_host(signatures[-1][1])):
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
