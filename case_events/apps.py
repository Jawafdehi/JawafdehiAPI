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
