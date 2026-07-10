from django.apps import AppConfig


class NewsletterConfig(AppConfig):
    """Newsletter signup/unsubscribe proxy to the SendPulse ESP.

    This app owns no database models: SendPulse is the system of record for
    subscribers and runs its own double opt-in confirmation. The app exposes two
    public endpoints matching the frontend contract and forwards to SendPulse.
    """

    default_auto_field = "django.db.models.BigAutoField"
    name = "newsletter"
