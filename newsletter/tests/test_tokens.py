"""Unit tests for the stateless signed unsubscribe token."""

import pytest
from django.core import signing

from newsletter.tokens import make_unsubscribe_token, unsign_unsubscribe_token


def test_round_trip_recovers_email():
    token = make_unsubscribe_token("reader@example.org")
    assert unsign_unsubscribe_token(token) == "reader@example.org"


def test_tampered_token_rejected():
    token = make_unsubscribe_token("reader@example.org")
    with pytest.raises(signing.BadSignature):
        unsign_unsubscribe_token(token + "x")


def test_expired_token_rejected(settings):
    settings.NEWSLETTER_UNSUBSCRIBE_MAX_AGE_DAYS = 0
    token = make_unsubscribe_token("reader@example.org")
    # max_age of 0 means anything with age >= 0 is expired.
    with pytest.raises(signing.BadSignature):  # SignatureExpired subclasses BadSignature
        unsign_unsubscribe_token(token)


def test_foreign_signature_rejected():
    """A value signed with a different salt must not validate as a token."""
    foreign = signing.dumps({"email": "x@y.z"}, salt="some.other.salt")
    with pytest.raises(signing.BadSignature):
        unsign_unsubscribe_token(foreign)
