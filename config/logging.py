import logging
import structlog
from django.conf import settings

SERVICE_NAME = "jawafdehi-api"


def configure_structlog():
    timestamper = structlog.processors.TimeStamper(fmt="iso")

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        timestamper,
    ]

    if settings.DEBUG:
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

    root_handler = logging.StreamHandler()
    root_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [root_handler]
    root_logger.setLevel(logging.DEBUG if settings.DEBUG else logging.INFO)

    for _log_name in [
        "django",
        "django.request",
        "django.server",
        "django.template",
        "django.db.backends",
        "gunicorn",
    ]:
        logger = logging.getLogger(_log_name)
        logger.handlers = [root_handler]
        logger.propagate = False
