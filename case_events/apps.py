# SPDX-License-Identifier: Hippocratic-3.0
from django.apps import AppConfig


class EventsConfig(AppConfig):
    """The case-enrichment event bus.

    Registered as an app for management-command discovery (the consumer runner
    lands here). It has no models and therefore no migrations, and it opens no
    connection at startup — the bus connects lazily on first publish.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "case_events"

    def ready(self):
        # Connects the Material post_save producer. Imported for its side effect,
        # and guarded: a producer that fails to register must not take down the
        # app that also owns the consumers and the bootstrap command, since those
        # are what you would use to diagnose it.
        #
        # This opens no connection. With NATS_URL unset every emit is a logged
        # no-op and no thread is started, so registering it costs nothing until
        # the broker exists.
        try:
            from case_events.producers import materials  # noqa: F401
        except Exception:  # noqa: BLE001
            import structlog

            structlog.get_logger(__name__).exception("case_events.producer_registration_failed")
