"""The ``prerender`` role must buy a throttle bucket and nothing else.

The jawafdehi.org build authenticates as a machine user holding this role so its
reads stop competing with visitors for the 1000/hour anonymous bucket. Whatever
it fetches is written into static HTML that is then served to everyone — so the
moment this role can see something an anonymous caller cannot, the build starts
publishing non-public data, silently, to crawlers.

Today that holds for a structural reason rather than an enforced one: every gate
in the codebase is an allowlist of group NAMES, and ``Prerender`` is in none of
them. These tests pin it, because "it works because nobody listed us" is exactly
the kind of property a future allowlist edit breaks without noticing.

The companion check lives in the throttle tests at the bottom: the role has to
actually route somewhere, or we would have taken on a credential for nothing.
"""

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIRequestFactory

from cases.models import CaseState
from cases.rules.predicates import (
    can_change_case,
    can_view_case,
    has_role,
    is_admin_or_moderator,
    is_caseworker,
    is_readonly,
)
from cases.serializers import _viewer_has_casework_access
from courts.permissions import NGM_QUERY_GROUPS, NGM_ROLE_GROUPS
from jawafdehi_shared.auth.oidc import DEFAULT_ROLE_TO_GROUP
from jawafdehi_shared.drf.throttling import (
    PRERENDER_GROUP,
    PRERENDER_SCOPE,
    SyncedUserRateThrottle,
)
from materials.views import _NONPUBLIC_READ_GROUPS
from tests.conftest import create_case_with_entities

User = get_user_model()


@pytest.fixture
def prerender_user(db):
    """A principal holding the prerender role and nothing else."""
    user = User.objects.create_user(
        username="393507336624276852",  # machine users are keyed by OIDC sub
        email="",
        password="testpass123",
    )
    user.groups.add(Group.objects.get_or_create(name=PRERENDER_GROUP)[0])
    return user


# ============================================================================
# The wiring: Zitadel role -> Django group
# ============================================================================


def test_prerender_role_maps_to_the_prerender_group():
    assert DEFAULT_ROLE_TO_GROUP["prerender"] == PRERENDER_GROUP


def test_prerender_role_does_not_share_a_group_with_a_content_role():
    """A typo collapsing it onto Caseworker/ReadOnly would hand it the archive."""
    assert PRERENDER_GROUP not in {"Caseworker", "ReadOnly", "JobPoller"}


@pytest.mark.django_db
def test_create_groups_grants_the_prerender_group_nothing():
    call_command("create_groups")

    group = Group.objects.get(name=PRERENDER_GROUP)
    assert list(group.permissions.all()) == []


@pytest.mark.django_db
def test_create_groups_strips_permissions_granted_by_hand():
    """``.set([])`` is unconditional, so a hand-granted perm does not survive."""
    call_command("create_groups")
    group = Group.objects.get(name=PRERENDER_GROUP)
    group.permissions.set(Group.objects.get(name="ReadOnly").permissions.all())
    assert group.permissions.exists()  # guard: the setup actually granted something

    call_command("create_groups")

    assert list(Group.objects.get(name=PRERENDER_GROUP).permissions.all()) == []


# ============================================================================
# The invariant: the role is admitted by no gate
# ============================================================================


@pytest.mark.django_db
def test_prerender_is_not_a_content_or_read_role(prerender_user):
    assert is_caseworker(prerender_user) is False
    assert is_admin_or_moderator(prerender_user) is False
    assert is_readonly(prerender_user) is False
    assert has_role(prerender_user) is False


@pytest.mark.django_db
def test_prerender_cannot_view_a_draft_case(prerender_user):
    """The one that matters: a draft must not reach a public static file."""
    draft = create_case_with_entities(title="Draft", state=CaseState.DRAFT)

    assert can_view_case(prerender_user, draft) is False


@pytest.mark.django_db
def test_prerender_cannot_change_a_case(prerender_user):
    case = create_case_with_entities(title="Published", state=CaseState.PUBLISHED)

    assert can_change_case(prerender_user, case) is False


@pytest.mark.django_db
def test_prerender_gets_the_public_serializer_payload(prerender_user):
    """Internal casework notes are gated on the same pair of content roles.

    If this flips, every published case's internal notes get baked into the
    static HTML for that case.
    """
    request = APIRequestFactory().get("/api/cases/")
    request.user = prerender_user

    assert _viewer_has_casework_access({"request": request}) is False


def test_prerender_is_in_no_group_allowlist():
    """The structural reason the gates above refuse it, asserted directly.

    These three frozensets are what `/api/query/`, the NGM write gate and
    non-public material visibility are keyed on. Adding "Prerender" to any of
    them is the single edit that would undo this whole file.
    """
    assert PRERENDER_GROUP not in NGM_ROLE_GROUPS
    assert PRERENDER_GROUP not in NGM_QUERY_GROUPS
    assert PRERENDER_GROUP not in _NONPUBLIC_READ_GROUPS


# ============================================================================
# The thing it DOES buy: its own throttle bucket
# ============================================================================


@pytest.fixture
def rated_throttle(monkeypatch):
    """A throttle with rates configured.

    ``DEFAULT_THROTTLE_RATES`` is emptied under TESTING (config/settings.py), so
    constructing a scope-driven throttle raises ImproperlyConfigured unless the
    rates are supplied here — the same reason the feedback-scope tests assert on
    cache keys rather than on live counting.
    """
    monkeypatch.setattr(
        SyncedUserRateThrottle,
        "THROTTLE_RATES",
        {"anon": "1000/hour", "user": "5000/hour", PRERENDER_SCOPE: "10000/hour"},
    )
    cache.clear()
    yield SyncedUserRateThrottle()
    cache.clear()


def _get(user):
    request = APIRequestFactory().get("/api/cases/")
    request.user = user
    return request


@pytest.mark.django_db
def test_prerender_requests_land_in_the_prerender_bucket(prerender_user, rated_throttle):
    assert rated_throttle.allow_request(_get(prerender_user), view=None) is True

    assert rated_throttle.scope == PRERENDER_SCOPE
    assert rated_throttle.key == f"throttle_{PRERENDER_SCOPE}_{prerender_user.pk}"
    assert rated_throttle.num_requests == 10000


@pytest.mark.django_db
def test_an_ordinary_user_stays_in_the_shared_user_bucket(rated_throttle):
    user = User.objects.create_user(username="someone", email="", password="x")

    assert rated_throttle.allow_request(_get(user), view=None) is True

    assert rated_throttle.scope == "user"
    assert rated_throttle.key == f"throttle_user_{user.pk}"
    assert rated_throttle.num_requests == 5000


@pytest.mark.django_db
def test_the_two_buckets_are_separate_counters(prerender_user):
    """Scope drives the cache key, so neither can spend the other's allowance."""
    cache.clear()
    other = User.objects.create_user(username="someone-else", email="", password="x")

    pre = SyncedUserRateThrottle.__new__(SyncedUserRateThrottle)
    pre.scope = PRERENDER_SCOPE
    ordinary = SyncedUserRateThrottle.__new__(SyncedUserRateThrottle)
    ordinary.scope = "user"

    assert pre.get_cache_key(_get(prerender_user), view=None) != ordinary.get_cache_key(
        _get(other), view=None
    )


@pytest.mark.django_db
def test_no_prerender_rate_configured_falls_back_to_the_user_tier(
    prerender_user, monkeypatch
):
    """Dev and the test runner have no ``prerender`` rate; nothing should break."""
    monkeypatch.setattr(
        SyncedUserRateThrottle, "THROTTLE_RATES", {"user": "5000/hour"}
    )
    throttle = SyncedUserRateThrottle()

    assert throttle.allow_request(_get(prerender_user), view=None) is True

    assert throttle.scope == "user"


@pytest.mark.django_db
def test_the_group_lookup_is_cached_on_the_request(prerender_user, rated_throttle):
    """DRF rebuilds a throttle per request; the group query must not repeat.

    Every authenticated request pays this lookup, so without the memo it is one
    extra query per throttle per request on the hot path.
    """
    request = _get(prerender_user)
    rated_throttle.allow_request(request, view=None)
    assert request._jawafdehi_is_prerender is True

    with CaptureQueriesContext(connection) as ctx:
        second = SyncedUserRateThrottle()
        second.allow_request(request, view=None)

    assert second.scope == PRERENDER_SCOPE  # guard: it still routed correctly
    assert ctx.captured_queries == []
