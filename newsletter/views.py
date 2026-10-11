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
from django.template.loader import render_to_string
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import AnonRateThrottle
from rest_framework.views import APIView

from .sendpulse import SendPulseClient, SendPulseError, get_client
from .serializers import NewsletterSubscriptionSerializer

logger = logging.getLogger("newsletter.views")

# SendPulse returns 409 when the email already exists in the address book. We map
# that to a local 409 so the SPA can show its "previously unsubscribed" copy.
_SENDPULSE_CONFLICT_STATUS = 409


# One shared address book serves the newsletter and the Open House, so
# ``consent_source`` is what tells them apart — including which welcome to send.
# An Open House signup asked to be told about sessions; greeting them as a
# newsletter subscriber would answer a question they did not ask.
_OPEN_HOUSE_CONSENT_PREFIX = "openhouse"


def _welcome_for(consent_source: str) -> tuple[str, str]:
    """Pick the welcome template and subject for a signup's origin."""
    if consent_source.strip().lower().startswith(_OPEN_HOUSE_CONSENT_PREFIX):
        return (
            "newsletter/openhouse_welcome_email.html",
            getattr(
                settings,
                "SENDPULSE_OPEN_HOUSE_WELCOME_SUBJECT",
                "You're on the list — Jawafdehi Open House",
            ),
        )
    return (
        "newsletter/welcome_email.html",
        getattr(settings, "SENDPULSE_WELCOME_SUBJECT", "Welcome to Jawafdehi"),
    )


def _send_welcome_email(
    client: SendPulseClient, email: str, first_name: str, consent_source: str = ""
) -> None:
    """Best-effort welcome email on subscribe.

    Double opt-in isn't available via SendPulse's API for this account, so the
    subscribe is single opt-in and this transactional welcome is the "email send"
    step. It never fails the subscribe: any error is logged and swallowed.
    """
    if not getattr(settings, "SENDPULSE_WELCOME_EMAIL", False) or not client.can_send_email:
        return
    try:
        template, subject = _welcome_for(consent_source)
        html = render_to_string(template, {"first_name": first_name})
        client.send_email(email, subject, html, to_name=first_name)
    except Exception as exc:  # noqa: BLE001 — welcome is best-effort, never block subscribe
        logger.warning("Newsletter welcome email failed (subscribe still succeeded): %s", exc)


def _notify_signup(client: SendPulseClient, data: dict, *, deferred: bool) -> None:
    """Best-effort alert to the Jawafdehi inbox that someone signed up.

    This app keeps no rows, so SendPulse is the only record a signup ever leaves
    and the only way to notice one is to go and look at the dashboard. Nobody
    does, so signups go unnoticed — this mail is what makes them visible.

    ``deferred`` says the address book rejected the contact and the subscribe
    degraded to a local 202. That is the case this is most worth having: the
    subscriber was told they succeeded, nothing was stored anywhere, and this
    email is the sole surviving copy of their details.

    Unlike the case-report alert (``cases.api_views._notify_case_report``) this
    one carries the submission in full. The two are different in kind: a
    corruption report is a third party's account that must not leave the
    database, whereas a signup is the person's own contact details, given
    expressly so we can contact them — and already held by SendPulse, which is
    the service carrying this mail. There is no new exposure.

    Never raises: an alert is not worth failing somebody's signup over.
    """
    if not getattr(settings, "NEWSLETTER_SIGNUP_NOTIFY", False):
        return
    recipient = str(getattr(settings, "NEWSLETTER_SIGNUP_NOTIFY_EMAIL", "") or "").strip()
    if not recipient or not client.can_send_email:
        return

    consent_source = data["consentSource"]
    source_label = (
        "Open House"
        if consent_source.strip().lower().startswith(_OPEN_HOUSE_CONSENT_PREFIX)
        else "Newsletter"
    )
    name = " ".join(
        part for part in [data["firstName"], data.get("lastName", "")] if part
    ).strip()
    try:
        # Only the fields that were actually filled in, so a newsletter signup
        # does not arrive padded with four blank Open House rows.
        details = [
            (label, value)
            for label, value in (
                ("Email", data["email"]),
                ("Region", data.get("region", "")),
                ("WhatsApp", data.get("whatsapp", "")),
                ("Organisation", data.get("organisation", "")),
                ("Language", data.get("locale", "")),
                ("Form", consent_source),
                ("Privacy version", data["privacyVersion"]),
            )
            if value
        ]
        html = render_to_string(
            "newsletter/signup_notification.html",
            {
                "source_label": source_label,
                "name": name,
                "email": data["email"],
                "deferred": deferred,
                "details": details,
            },
        )
        # Whitespace is collapsed because the subject is the one value that does
        # not pass through template autoescaping, and `firstName` is public input
        # that keeps its interior newlines through the serializer's trimming.
        subject_name = " ".join((name or data["email"]).split())
        subject = f"{source_label} signup: {subject_name}"
        if deferred:
            subject = f"[not saved] {subject}"
        client.send_email(recipient, subject, html)
    except Exception as exc:  # noqa: BLE001 — alert is best-effort, never block subscribe
        logger.warning("Signup notification failed (subscribe still succeeded): %s", exc)


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
            #
            # No signup alert is possible here: the alert sends *through*
            # SendPulse, and this branch is "SendPulse is not configured". That
            # is a deploy misconfiguration rather than an outage, so the log line
            # is the right surface for it — the outage case is handled below.
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
                    # The address book stores the full name as `name`, but every
                    # campaign template reads `{{first_name}}` — unset, that
                    # renders as a bare "Namaste ,". Send both.
                    "first_name": data["firstName"],
                    # Open House fields. Always sent, blank when absent, so a
                    # contact's shape does not depend on which form created it.
                    "region": data.get("region", ""),
                    "whatsapp": data.get("whatsapp", ""),
                    "organisation": data.get("organisation", ""),
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
            # "Deferred" overstates it: nothing retries this, and the signup is
            # held nowhere. The alert is the retry, and it goes through
            # /smtp/emails — a different endpoint from the one that just failed,
            # so it has its own chance of getting through.
            _notify_signup(client, data, deferred=True)
            return Response(
                {
                    "email": email,
                    "status": "accepted",
                    "message": "Subscription received.",
                },
                status=status.HTTP_202_ACCEPTED,
            )

        _send_welcome_email(client, email, data["firstName"], data["consentSource"])
        _notify_signup(client, data, deferred=False)

        return Response(
            {
                "email": email,
                "status": "subscribed",
                "message": "You're subscribed. Check your inbox for a welcome email.",
            },
            status=status.HTTP_201_CREATED,
        )
