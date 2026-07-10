"""Stateless, signed unsubscribe tokens.

The app stores no subscriber rows, so the unsubscribe token must carry the
subscriber's email itself. We use :mod:`django.core.signing` (HMAC over
``SECRET_KEY`` plus a dedicated salt) so a token is:

- **unforgeable** — a client can't mint a token for an arbitrary email, and
- **self-describing** — the unsubscribe view recovers the email without a DB
  lookup, then calls SendPulse to remove it.

Tokens carry an age: :func:`unsign_unsubscribe_token` rejects anything older than
``NEWSLETTER_UNSUBSCRIBE_MAX_AGE_DAYS`` (default 365 days) so a leaked link in an
old email eventually stops working.
"""

from __future__ import annotations

from django.conf import settings
from django.core import signing

_SALT = "newsletter.unsubscribe"


def _max_age_seconds() -> int:
    days = int(getattr(settings, "NEWSLETTER_UNSUBSCRIBE_MAX_AGE_DAYS", 365))
    return days * 24 * 60 * 60


def make_unsubscribe_token(email: str) -> str:
    """Return an opaque, signed token that encodes ``email``."""
    return signing.dumps({"email": email}, salt=_SALT)


def unsign_unsubscribe_token(token: str) -> str:
    """Recover the email from a token, or raise ``signing.BadSignature``.

    Raises :class:`django.core.signing.SignatureExpired` when the token is older
    than the configured max age, and :class:`django.core.signing.BadSignature`
    for a tampered/invalid token or an unexpected payload shape.
    """
    data = signing.loads(token, salt=_SALT, max_age=_max_age_seconds())
    if not isinstance(data, dict) or not data.get("email"):
        raise signing.BadSignature("Unsubscribe token payload is malformed.")
    return data["email"]
