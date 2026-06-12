"""Tests for the role-aware global default throttle.

The ``TESTING`` guard in settings strips ``DEFAULT_THROTTLE_RATES`` during the
suite, so each test supplies its own rates via ``override_settings`` (DRF's
``api_settings`` reloads on the ``setting_changed`` signal).
"""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group
from django.test import override_settings
from rest_framework.test import APIRequestFactory

from cases.throttles import RoleBasedUserRateThrottle

User = get_user_model()

RATES = {
    "anon": "100/hour",
    "user": "1000/hour",
    "contributor": "2500/hour",
    "staff": "5000/hour",
}


def add_user_to_groups(user, *group_names):
    for group_name in group_names:
        group, _ = Group.objects.get_or_create(name=group_name)
        user.groups.add(group)


@override_settings(REST_FRAMEWORK={"DEFAULT_THROTTLE_RATES": RATES})
@pytest.mark.django_db
@pytest.mark.parametrize(
    ("groups", "is_staff", "is_superuser", "expected_rate"),
    [
        ([], False, False, "1000/hour"),
        (["Admin"], False, False, "5000/hour"),
        (["Moderator"], False, False, "5000/hour"),
        (["Contributor"], False, False, "2500/hour"),
        ([], True, False, "5000/hour"),
        ([], False, True, "5000/hour"),
        # Staff group wins over contributor regardless of group set order.
        (["Contributor", "Admin"], False, False, "5000/hour"),
        # ReadOnly is not a staff/contributor tier -> stays at the user default.
        (["ReadOnly"], False, False, "1000/hour"),
    ],
)
def test_get_user_rate_by_role(groups, is_staff, is_superuser, expected_rate):
    user = User.objects.create_user(
        username="rate_user",
        password="testpass123",
        is_staff=is_staff,
        is_superuser=is_superuser,
    )
    add_user_to_groups(user, *groups)

    throttle = RoleBasedUserRateThrottle()

    assert throttle.get_user_rate(user) == expected_rate


@override_settings(REST_FRAMEWORK={"DEFAULT_THROTTLE_RATES": RATES})
def test_anonymous_user_gets_default_rate():
    throttle = RoleBasedUserRateThrottle()

    assert throttle.get_user_rate(AnonymousUser()) == "1000/hour"
    assert throttle.get_user_rate(None) == "1000/hour"


@pytest.mark.django_db
def test_authenticated_users_bucket_by_identity_not_ip():
    """Two users sharing one IP must get independent cache buckets.

    Regression guard: the global throttle must key authenticated callers by
    user identity (like DRF's UserRateThrottle), not by client IP. JWT/Session
    auth leave request.auth without a token key, so an IP fallback would make
    users behind a shared NAT/proxy share a quota.
    """
    factory = APIRequestFactory()
    throttle = RoleBasedUserRateThrottle()
    throttle.scope = "user"

    user_a = User.objects.create_user(username="user_a", password="testpass123")
    user_b = User.objects.create_user(username="user_b", password="testpass123")

    # Same client IP, different authenticated users.
    req_a = factory.get("/api/anything/", REMOTE_ADDR="10.0.0.1")
    req_a.user = user_a
    req_a.auth = None  # JWT/Session: no DRF token key on request.auth
    req_b = factory.get("/api/anything/", REMOTE_ADDR="10.0.0.1")
    req_b.user = user_b
    req_b.auth = None

    key_a = throttle.get_cache_key(req_a, view=None)
    key_b = throttle.get_cache_key(req_b, view=None)

    assert key_a != key_b
    assert str(user_a.pk) in key_a
    assert str(user_b.pk) in key_b


@pytest.mark.django_db
def test_anonymous_requests_bucket_by_ip():
    factory = APIRequestFactory()
    throttle = RoleBasedUserRateThrottle()
    throttle.scope = "user"

    request = factory.get("/api/anything/", REMOTE_ADDR="10.0.0.9")
    request.user = AnonymousUser()
    request.auth = None

    assert throttle.get_cache_key(request, view=None) == throttle.cache_format % {
        "scope": "user",
        "ident": "10.0.0.9",
    }
