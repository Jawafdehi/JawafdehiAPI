import os

import structlog

SHARED_CONTEXT_VARS = [
    "request_id",
    "entity_id",
    "case_id",
    "revision_id",
    "service",
]


def add_service_name(_, __, event_dict):
    event_dict.setdefault("service", os.getenv("SERVICE_NAME", "jawafdehi-api"))
    return event_dict


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
            (
                structlog.dev.ConsoleRenderer()
                if os.getenv("STRUCTLOG_CONSOLE", "1") == "1"
                else structlog.processors.JSONRenderer()
            ),
        ],
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
