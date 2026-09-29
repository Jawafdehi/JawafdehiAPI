"""Request serializers for the newsletter endpoints.

Plain (non-model) serializers: this app has no database models. Field names are
camelCase to match the frontend payload verbatim
(``src/services/jds-api.ts`` → ``NewsletterSubscription``).
"""

from __future__ import annotations

import re

from rest_framework import serializers

# Regions are the slots a session can be scheduled into, not geography for its
# own sake: the Open House time moves week to week to suit whoever is coming, so
# this is what makes "invite the people a 20:00 NPT slot actually works for"
# possible. A closed list rather than free text, because the whole point is
# filtering the list later and free text does not filter.
#
# Keep in lockstep with the dropdown in jawafdehi-frontend
# ``src/components/open-house/regions.ts``.
OPEN_HOUSE_REGIONS = (
    "nepal-south-asia",
    "gulf-middle-east",
    "east-southeast-asia",
    "europe-africa",
    "australia-nz",
    "north-america-east",
    "north-america-west",
)

# Deliberately forgiving: digits with the separators people actually type, and an
# optional leading +. Anything stricter rejects real numbers and the field is
# optional anyway — a rejected signup costs more than a slightly messy number.
#
# ⚠️ `0-9`, never `\d`. Python's `\d` matches Unicode digits, so `\d` here would
# accept Devanagari numerals and "normalise" ९८४१२३४५६७ into a number nobody can
# dial — a realistic way to collect unusable contacts on a Nepali-first site.
# Devanagari input is transliterated first (below) rather than rejected, so
# someone typing their own numerals still gets a working number out.
_WHATSAPP_ALLOWED = re.compile(r"^\+?[0-9\s().-]{5,31}$")
_WHATSAPP_STRIP = re.compile(r"[^0-9+]")

# U+0966..U+096F, in order, so str.translate maps them onto ASCII.
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


class NewsletterSubscriptionSerializer(serializers.Serializer):
    """Validates a subscribe request from the SPA.

    Mirrors the frontend ``NewsletterSubscription`` shape:
    ``{email, firstName, lastName?, consentAccepted, consentSource,
    privacyVersion, locale?, region?, whatsapp?, organisation?}``.

    The last three arrived with the Open House and are optional everywhere: the
    newsletter forms do not send them, and an Open House signup only has to
    supply an email.
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
    region = serializers.ChoiceField(
        choices=OPEN_HOUSE_REGIONS, required=False, allow_blank=True
    )
    whatsapp = serializers.CharField(
        max_length=32, required=False, allow_blank=True, trim_whitespace=True
    )
    organisation = serializers.CharField(
        max_length=200, required=False, allow_blank=True, trim_whitespace=True
    )

    def validate_whatsapp(self, value: str) -> str:
        """Normalise to ``+`` and digits so the stored numbers are dialable.

        Returned blank for a blank input rather than rejected — the field is
        optional and someone who leaves it empty is not making a mistake.
        """
        if not value:
            return ""
        # Someone writing their own number in Devanagari is not making a mistake,
        # so transliterate before validating rather than turning them away.
        value = value.translate(_DEVANAGARI_DIGITS)
        if not _WHATSAPP_ALLOWED.match(value):
            raise serializers.ValidationError(
                "Enter a WhatsApp number using digits, spaces, brackets, "
                "hyphens and an optional leading +."
            )
        normalized = _WHATSAPP_STRIP.sub("", value)
        # A stray '+' anywhere but the front is a typo, not a country code.
        if "+" in normalized[1:]:
            raise serializers.ValidationError("A '+' may only lead the number.")
        if len(normalized.lstrip("+")) < 5:
            raise serializers.ValidationError("That number looks too short.")
        return normalized

    def validate_consentAccepted(self, value: bool) -> bool:
        """Consent is mandatory — an unconsented submit is a 400, not a store."""
        if value is not True:
            raise serializers.ValidationError(
                "Consent is required to subscribe to the newsletter."
            )
        return value

    def validate_email(self, value: str) -> str:
        return value.strip().lower()
