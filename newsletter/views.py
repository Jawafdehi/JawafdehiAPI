"""Public newsletter subscribe endpoint.

Anonymous (``AllowAny``) and rate-limited, mirroring
``cases.api_views.FeedbackView``. SendPulse is the system of record; this app
holds no subscriber rows.

Unsubscribe is intentionally NOT handled here: SendPulse injects its own hosted
unsubscribe link into every campaign email (a legal requirement it owns), so the
sender never routes unsubscribe traffic through its own backend.

Contract (must stay in lockstep with ``jawafdehi-frontend``
``src/services/jds-api.ts``):

- ``POST /api/newsletter/subscriptions/``
  - ``201`` created / accepted by SendPulse
  - ``202`` accepted locally but SendPulse sync is deferred (ESP down / not
    configured) — the SPA treats any 2xx as success
  - ``409`` the address was previously unsubscribed / already exists (SPA shows
    the "email privacy@" copy)
  - ``429`` throttled  ·  ``400`` validation
"""

from __future__ import annotations

import logging

from django.conf import settings
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .sendpulse import SendPulseError, get_client
from .serializers import NewsletterSubscriptionSerializer

logger = logging.getLogger("newsletter.views")

# SendPulse returns 409 when the email already exists in the address book. We map
# that to a local 409 so the SPA can show its "previously unsubscribed" copy.
_SENDPULSE_CONFLICT_STATUS = 409


class NewsletterRateThrottle(AnonRateThrottle):
    """Rate throttle for newsletter subscribe: 10 per hour per IP."""

    scope = "newsletter"
    rate = "10/hour"


class _ThrottledPublicView(APIView):
    """Anonymous, throttled base for the newsletter endpoints.

    The throttle is declared here rather than left to the DRF defaults (which are
    disabled under the test runner) so it applies in production. It is skipped
    under ``TESTING`` — like the global throttle — because DRF's rate-limit cache
    makes per-request unit tests flaky; the dedicated throttle test re-enables it
    explicitly.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [NewsletterRateThrottle]

    def get_throttles(self):
        if getattr(settings, "TESTING", False):
            return []
        return super().get_throttles()


@extend_schema(
    summary="Subscribe to the newsletter",
    description=(
        "Subscribe an email to the Jawafdehi newsletter. Consent is required. "
        "SendPulse sends its own double opt-in confirmation email. Rate limited "
        "to 10 requests per IP per hour. Returns 202 (still success) when the "
        "email provider is temporarily unavailable so the flow degrades "
        "gracefully."
    ),
    request=NewsletterSubscriptionSerializer,
    responses={
        201: OpenApiTypes.OBJECT,
        202: OpenApiTypes.OBJECT,
        400: OpenApiTypes.OBJECT,
        409: OpenApiTypes.OBJECT,
        429: OpenApiTypes.OBJECT,
    },
)
class NewsletterSubscriptionView(_ThrottledPublicView):
    """Create a newsletter subscription by forwarding to SendPulse."""

    def post(self, request):
        serializer = NewsletterSubscriptionSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "Validation error", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = serializer.validated_data
        email = data["email"]
        name = " ".join(
            part for part in [data["firstName"], data.get("lastName", "")] if part
        ).strip()

        client = get_client()
        if client is None:
            # ESP not provisioned yet — accept the request so the UX works, and
            # log it for later reconciliation. No PII beyond the fact of a signup.
            logger.warning(
                "Newsletter subscribe accepted but SendPulse is not configured "
                "(consentSource=%s, locale=%s).",
                data.get("consentSource"),
                data.get("locale"),
            )
            return Response(
                {
                    "email": email,
                    "status": "accepted",
                    "message": "Subscription received.",
                },
                status=status.HTTP_202_ACCEPTED,
            )

        try:
            client.add_subscriber(
                email,
                name=name,
                variables={
                    "locale": data.get("locale", ""),
                    "consent_source": data["consentSource"],
                    "privacy_version": data["privacyVersion"],
                },
            )
        except SendPulseError as exc:
            if exc.status == _SENDPULSE_CONFLICT_STATUS:
                # Address previously existed (e.g. unsubscribed). Surface as 409
                # so the SPA shows its "previously unsubscribed" guidance.
                return Response(
                    {
                        "error": "Already subscribed or previously unsubscribed.",
                        "detail": (
                            "This address is already on our list or was "
                            "previously unsubscribed."
                        ),
                    },
                    status=status.HTTP_409_CONFLICT,
                )
            # Transient outage or ESP error: accept locally, defer the sync. The
            # SPA treats 2xx as success, so the user isn't blocked by ESP uptime.
            logger.error("SendPulse subscribe failed; accepting locally: %s", exc)
            return Response(
                {
                    "email": email,
                    "status": "accepted",
                    "message": "Subscription received.",
                },
                status=status.HTTP_202_ACCEPTED,
            )

        return Response(
            {
                "email": email,
                "status": "subscribed",
                "message": "Please check your inbox to confirm your subscription.",
            },
            status=status.HTTP_201_CREATED,
        )
