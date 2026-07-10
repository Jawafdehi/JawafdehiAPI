from django.apps import AppConfig


class NewsletterConfig(AppConfig):
    """Newsletter signup proxy to the SendPulse ESP.

    This app owns no database models: SendPulse is the system of record for
    subscribers, runs its own double opt-in confirmation, and hosts the
    unsubscribe link in its emails. The app exposes the public subscribe endpoint
    matching the frontend contract and forwards to SendPulse.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "newsletter"
