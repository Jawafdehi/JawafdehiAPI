"""The feedback throttle must count in its own bucket, not the global anon one.

DRF's ``SimpleRateThrottle`` derives its cache key from ``scope`` alone
(``throttle_<scope>_<ident>``). ``FeedbackRateThrottle`` and the platform-wide
``SyncedAnonRateThrottle`` are both ``AnonRateThrottle`` subclasses, so if the
feedback one doesn't override ``scope`` they share the key — and the global
throttle, which runs on every other public endpoint at 1000/hour, spends the
feedback throttle's 5/hour allowance on ordinary browsing.

These tests assert on the cache key rather than trying to reproduce the
collision end-to-end, because the global throttle classes are emptied under
``TESTING`` (config/settings.py) and the two can never actually collide in the
suite. That is precisely why the bug survived: the only place it appears is
production.
"""

import pytest
from django.core.cache import cache
from rest_framework.test import APIRequestFactory

from cases.api_views import FeedbackRateThrottle
from jawafdehi_shared.drf.throttling import SyncedAnonRateThrottle


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()
    yield
    cache.clear()


IP = "203.0.113.7"


def _request():
    request = APIRequestFactory().post("/api/feedback/", REMOTE_ADDR=IP)
    # DRF's View wrapper normally supplies request.user; AnonRateThrottle only
    # reads `.user` to decide whether the caller is anonymous.
    request.user = None
    return request


def test_feedback_throttle_has_its_own_scope():
    assert FeedbackRateThrottle.scope == "feedback"


def test_feedback_throttle_scope_differs_from_the_global_anon_bucket():
    """The regression, asserted on the class attributes.

    Compared as class attributes rather than by instantiating the global
    throttle: ``SyncedAnonRateThrottle`` has no hardcoded ``rate``, so under
    ``TESTING`` (where ``DEFAULT_THROTTLE_RATES`` is ``{}``) constructing one
    raises ImproperlyConfigured. The scope is what determines the cache key, so
    comparing scopes is the honest assertion anyway.
    """
    assert FeedbackRateThrottle.scope != SyncedAnonRateThrottle.scope
    assert SyncedAnonRateThrottle.scope == "anon"


def test_feedback_cache_key_is_not_the_shared_anon_key():
    """The concrete consequence: a different bucket for the same client."""
    key = FeedbackRateThrottle().get_cache_key(_request(), view=None)

    assert key == f"throttle_feedback_{IP}"
    assert key != f"throttle_anon_{IP}", (
        "feedback submissions would be counted in the global anonymous bucket, "
        "so ordinary browsing consumes the reporter's 5/hour allowance"
    )


def test_feedback_throttle_keeps_its_rate():
    """The scope must not send DRF looking up THROTTLE_RATES['feedback'].

    ``SimpleRateThrottle.__init__`` only calls ``get_rate()`` when ``rate`` is
    unset; the explicit class attribute keeps 5/hour without needing a settings
    entry (same arrangement as NewsletterRateThrottle).
    """
    throttle = FeedbackRateThrottle()
    assert throttle.rate == "5/hour"
    assert throttle.num_requests == 5
    assert throttle.duration == 3600
