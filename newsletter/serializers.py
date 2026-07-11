"""Request serializers for the newsletter endpoints.

Plain (non-model) serializers: this app has no database models. Field names are
camelCase to match the frontend payload verbatim
(``src/services/jds-api.ts`` → ``NewsletterSubscription``).
"""

from __future__ import annotations

from rest_framework import serializers


class NewsletterSubscriptionSerializer(serializers.Serializer):
    """Validates a subscribe request from the SPA.

    Mirrors the frontend ``NewsletterSubscription`` shape:
    ``{email, firstName, lastName?, consentAccepted, consentSource,
    privacyVersion, locale?}``.
    """

    email = serializers.EmailField()
    firstName = serializers.CharField(max_length=150, trim_whitespace=True)
    lastName = serializers.CharField(
        max_length=150, required=False, allow_blank=True, trim_whitespace=True
    )
    consentAccepted = serializers.BooleanField()
    consentSource = serializers.CharField(max_length=100)
    privacyVersion = serializers.CharField(max_length=50)
    locale = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_consentAccepted(self, value: bool) -> bool:
        """Consent is mandatory — an unconsented submit is a 400, not a store."""
        if value is not True:
            raise serializers.ValidationError(
                "Consent is required to subscribe to the newsletter."
            )
        return value

    def validate_email(self, value: str) -> str:
        return value.strip().lower()
