"""Tests for jawafdehi_shared.auth.oidc.OIDCAuthentication (Zitadel OIDC resource-server).

These tests do NOT require a live Zitadel instance: they mint RS256 tokens with
a self-signed RSA key generated in-process and stub PyJWKClient so the
authenticator validates against that key. They cover:

  (a) a valid token -> a real Django user whose Groups are synced from the
      Zitadel project-roles claim,
  (b) bad-signature / expired / wrong-audience / wrong-issuer rejection,
  (c) the role -> Django Group mapping (including the docs' array-wrapped claim
      shape and ignoring unknown roles).
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from rest_framework import exceptions
from rest_framework.test import APIRequestFactory

from jawafdehi_shared.auth import oidc as oidc_auth

User = get_user_model()

ISSUER = "https://auth.test.jawafdehi.org"
AUDIENCE = "123456789"  # stand-in for the Zitadel project ID
ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"

# Groups the role mapping references; created per test so the sync can attach
# them. Mirrors create_groups.py / the predicate group names.
#
# v3 authz model: the only Django groups are Caseworker (the single content-staff
# role), ReadOnly and JobPoller. Admin is is_superuser (no group); the old
# Moderator/Public groups and the NGM_* tiers are retired.
ALL_GROUPS = [
    "Caseworker",
    "ReadOnly",
    "JobPoller",
]


# ---------------------------------------------------------------------------
# Key / token helpers
# ---------------------------------------------------------------------------
def _gen_rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


# One signing key shared across the module (cheap: generated once).
_SIGNING_KEY = _gen_rsa_key()
_WRONG_KEY = _gen_rsa_key()


def _make_token(
    *,
    key=_SIGNING_KEY,
    sub="user-sub-1",
    aud=AUDIENCE,
    iss=ISSUER,
    roles=None,
    email="user@example.org",
    exp_delta=timedelta(minutes=5),
    extra_claims=None,
):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "aud": aud,
        "iss": iss,
        "iat": now,
        "exp": now + exp_delta,
        "email": email,
    }
    if roles is not None:
        payload[ROLES_CLAIM] = roles
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, key, algorithm="RS256")


class _FakeSigningKey:
    """Mimics jwt.PyJWK enough for OIDCAuthentication (it reads `.key`)."""

    def __init__(self, public_key):
        self.key = public_key


class _FakeJWKClient:
    """Stand-in for PyJWKClient that always returns the test public key."""

    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):  # noqa: D401 - signature parity
        return _FakeSigningKey(self._public_key)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _oidc_settings(settings):
    """Configure OIDC settings and stub the JWKS client for every test."""
    settings.OIDC_ISSUER = ISSUER
    settings.OIDC_AUDIENCE = AUDIENCE
    settings.OIDC_JWKS_URI = f"{ISSUER}/oauth/v2/keys"
    settings.OIDC_ROLES_CLAIM = ROLES_CLAIM
    settings.OIDC_ALGORITHMS = ["RS256"]
    settings.OIDC_LEEWAY = 30
    # Use the default role->group mapping from the module.
    settings.OIDC_ROLE_TO_GROUP = oidc_auth.DEFAULT_ROLE_TO_GROUP

    # Inject the fake JWKS client (signs with the test public key).
    oidc_auth.reset_jwks_client()
    oidc_auth._jwks_client = _FakeJWKClient(_SIGNING_KEY.public_key())
    yield
    oidc_auth.reset_jwks_client()


@pytest.fixture
def groups(db):
    for name in ALL_GROUPS:
        Group.objects.get_or_create(name=name)


def _authenticate(token):
    factory = APIRequestFactory()
    request = factory.get("/api/", HTTP_AUTHORIZATION=f"Bearer {token}")
    return oidc_auth.OIDCAuthentication().authenticate(request)


# ---------------------------------------------------------------------------
# (a) valid token -> authed user with groups synced from roles
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_valid_token_authenticates_and_syncs_groups(groups):
    token = _make_token(
        sub="zitadel-sub-abc",
        roles={"caseworker": {"222": "jawafdehi.org"}},
    )
    user, claims = _authenticate(token)

    assert user.username == "zitadel-sub-abc"
    assert user.is_active is True
    assert user.email == "user@example.org"
    assert claims["sub"] == "zitadel-sub-abc"
    assert set(user.groups.values_list("name", flat=True)) == {"Caseworker"}


@pytest.mark.django_db
def test_valid_token_creates_user_once_and_resyncs_groups(groups):
    # First request: contributor.
    t1 = _make_token(sub="sub-x", roles={"caseworker": {"1": "d"}})
    user1, _ = _authenticate(t1)
    assert User.objects.filter(username="sub-x").count() == 1
    assert set(user1.groups.values_list("name", flat=True)) == {"Caseworker"}

    # Second request for the same sub with different roles: same user, groups
    # overwritten (Zitadel is source of truth). In v3 ``admin`` maps to no group
    # (it only sets is_superuser); ``readonly`` maps to the ReadOnly group.
    t2 = _make_token(sub="sub-x", roles={"admin": {"1": "d"}, "readonly": {"1": "d"}})
    user2, _ = _authenticate(t2)
    assert user2.pk == user1.pk
    assert User.objects.filter(username="sub-x").count() == 1
    assert set(user2.groups.values_list("name", flat=True)) == {"ReadOnly"}
    assert user2.is_superuser is True


@pytest.mark.django_db
def test_no_roles_claim_results_in_no_groups(groups):
    token = _make_token(sub="sub-noroles", roles=None)
    user, _ = _authenticate(token)
    assert user.groups.count() == 0


# ---------------------------------------------------------------------------
# (b) rejection cases
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_bad_signature_rejected(groups):
    # Signed with a key that does NOT match the JWKS public key.
    token = _make_token(key=_WRONG_KEY, roles={"caseworker": {"1": "d"}})
    with pytest.raises(exceptions.AuthenticationFailed):
        _authenticate(token)
    assert not User.objects.filter(username="user-sub-1").exists()


@pytest.mark.django_db
def test_expired_token_rejected(groups):
    token = _make_token(exp_delta=timedelta(seconds=-300))
    with pytest.raises(exceptions.AuthenticationFailed) as exc:
        _authenticate(token)
    assert "expired" in str(exc.value).lower()


@pytest.mark.django_db
def test_wrong_audience_rejected(groups):
    token = _make_token(aud="some-other-project")
    with pytest.raises(exceptions.AuthenticationFailed) as exc:
        _authenticate(token)
    assert "audience" in str(exc.value).lower()


@pytest.mark.django_db
def test_wrong_issuer_rejected(groups):
    token = _make_token(iss="https://evil.example.com")
    with pytest.raises(exceptions.AuthenticationFailed) as exc:
        _authenticate(token)
    assert "issuer" in str(exc.value).lower()


@pytest.mark.django_db
def test_missing_required_claim_rejected(groups):
    # No `sub` claim -> options={"require": [...]} should reject.
    token = _make_token(roles={"caseworker": {"1": "d"}})
    # Re-mint without sub by decoding-free construction:
    now = datetime.now(timezone.utc)
    payload = {
        "aud": AUDIENCE,
        "iss": ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    token = jwt.encode(payload, _SIGNING_KEY, algorithm="RS256")
    with pytest.raises(exceptions.AuthenticationFailed):
        _authenticate(token)


@pytest.mark.django_db
def test_non_bearer_header_returns_none(groups):
    factory = APIRequestFactory()
    request = factory.get("/api/", HTTP_AUTHORIZATION="Token abc123")
    assert oidc_auth.OIDCAuthentication().authenticate(request) is None


@pytest.mark.django_db
def test_missing_header_returns_none(groups):
    factory = APIRequestFactory()
    request = factory.get("/api/")
    assert oidc_auth.OIDCAuthentication().authenticate(request) is None


@pytest.mark.django_db
def test_malformed_bearer_header_rejected(groups):
    factory = APIRequestFactory()
    request = factory.get("/api/", HTTP_AUTHORIZATION="Bearer")
    with pytest.raises(exceptions.AuthenticationFailed):
        oidc_auth.OIDCAuthentication().authenticate(request)


# ---------------------------------------------------------------------------
# (c) role -> group mapping
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_multiple_roles_map_to_multiple_groups(groups):
    # v3 keys: moderator + readonly collapse/map to {Caseworker, ReadOnly}.
    token = _make_token(
        sub="multi",
        roles={
            "moderator": {"1": "d"},
            "readonly": {"1": "d"},
        },
    )
    user, _ = _authenticate(token)
    assert set(user.groups.values_list("name", flat=True)) == {
        "Caseworker",
        "ReadOnly",
    }


@pytest.mark.django_db
def test_unknown_roles_ignored(groups):
    token = _make_token(
        sub="unknown-roles",
        roles={"caseworker": {"1": "d"}, "not_a_real_role": {"1": "d"}},
    )
    user, _ = _authenticate(token)
    assert set(user.groups.values_list("name", flat=True)) == {"Caseworker"}


@pytest.mark.django_db
def test_array_wrapped_roles_claim_normalized(groups):
    # Zitadel docs sometimes render the claim array-wrapped; extract_role_keys
    # normalizes both shapes.
    # v3: moderator and caseworker both collapse to the single Caseworker group.
    token = _make_token(
        sub="array-roles",
        roles=[{"moderator": {"1": "d"}}, {"caseworker": {"1": "d"}}],
    )
    user, _ = _authenticate(token)
    assert set(user.groups.values_list("name", flat=True)) == {
        "Caseworker",
    }


def test_extract_role_keys_normalizes_shapes(settings):
    settings.OIDC_ROLES_CLAIM = ROLES_CLAIM
    # plain map
    assert oidc_auth.extract_role_keys(
        {ROLES_CLAIM: {"admin": {}, "caseworker": {}}}
    ) == {"admin", "caseworker"}
    # array-wrapped
    assert oidc_auth.extract_role_keys(
        {ROLES_CLAIM: [{"admin": {}}, {"moderator": {}}]}
    ) == {"admin", "moderator"}
    # missing / empty
    assert oidc_auth.extract_role_keys({}) == set()
    assert oidc_auth.extract_role_keys({ROLES_CLAIM: None}) == set()


def test_extract_role_keys_reads_per_project_claim(settings):
    settings.OIDC_ROLES_CLAIM = ROLES_CLAIM
    settings.OIDC_AUDIENCE = AUDIENCE
    # A machine user's client-credentials token (e.g. the jobs-processor SA):
    # the urn:zitadel:iam:org:projects:roles scope yields ONLY the per-project
    # claim — the generic claim is absent even with projectRoleAssertion on.
    machine_claims = {
        f"urn:zitadel:iam:org:project:{AUDIENCE}:roles": {
            "review_assistant": {"377588697018728812": "zitadel.auth.jawafdehi.org"}
        }
    }
    assert oidc_auth.extract_role_keys(machine_claims) == {"review_assistant"}
    # Generic + own-project merge; unrelated urn claims are ignored.
    assert oidc_auth.extract_role_keys(
        {
            ROLES_CLAIM: {"caseworker": {}},
            f"urn:zitadel:iam:org:project:{AUDIENCE}:roles": {"review_assistant": {}},
            "urn:zitadel:iam:user:metadata": {"ignored": "x"},
        }
    ) == {"caseworker", "review_assistant"}


def test_extract_role_keys_ignores_sibling_project_claims(settings):
    settings.OIDC_ROLES_CLAIM = ROLES_CLAIM
    settings.OIDC_AUDIENCE = AUDIENCE
    # A token can pass the audience check while carrying role claims from
    # OTHER projects in the same org (extra audiences are accepted). Those
    # must never grant privileges here — 'admin' elsewhere is not admin here.
    claims = {
        f"urn:zitadel:iam:org:project:{AUDIENCE}:roles": {"review_assistant": {}},
        "urn:zitadel:iam:org:project:999999:roles": {"admin": {}, "caseworker": {}},
    }
    assert oidc_auth.extract_role_keys(claims) == {"review_assistant"}
    # A list-typed OIDC_AUDIENCE trusts each listed project id.
    settings.OIDC_AUDIENCE = [AUDIENCE, "999999"]
    assert oidc_auth.extract_role_keys(claims) == {
        "review_assistant",
        "admin",
        "caseworker",
    }


@pytest.mark.django_db
def test_synced_groups_satisfy_existing_predicates(groups):
    """Roles flowing from OIDC make the existing django-rules predicates pass."""
    from cases.rules.predicates import has_role, is_admin, is_caseworker

    token = _make_token(sub="pred-user", roles={"caseworker": {"1": "d"}})
    user, _ = _authenticate(token)

    # Re-fetch to clear any cached group relation.
    user = User.objects.get(pk=user.pk)
    assert is_caseworker(user) is True
    assert has_role(user) is True
    assert is_admin(user) is False


# ---------------------------------------------------------------------------
# (d) role model v2: admin -> is_superuser, public/readonly mapping
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_admin_role_sets_is_superuser(groups):
    """The ``admin`` role promotes is_superuser and attaches NO group (v3)."""
    token = _make_token(sub="su-1", roles={"admin": {"1": "d"}})
    user, _ = _authenticate(token)
    assert set(user.groups.values_list("name", flat=True)) == set()
    assert user.is_superuser is True


@pytest.mark.django_db
def test_admin_role_removal_clears_is_superuser(groups):
    """When the admin role is revoked, is_superuser is cleared on the next sync."""
    t1 = _make_token(sub="su-2", roles={"admin": {"1": "d"}})
    user1, _ = _authenticate(t1)
    assert user1.is_superuser is True

    # Same subject, admin role gone (now only caseworker): superuser dropped.
    t2 = _make_token(sub="su-2", roles={"caseworker": {"1": "d"}})
    user2, _ = _authenticate(t2)
    assert user2.pk == user1.pk
    user2 = User.objects.get(pk=user2.pk)
    assert user2.is_superuser is False
    assert set(user2.groups.values_list("name", flat=True)) == {"Caseworker"}


@pytest.mark.django_db
def test_non_admin_roles_do_not_set_is_superuser(groups):
    """readonly / public / caseworker must never confer superuser."""
    for role in ("readonly", "public", "caseworker"):
        token = _make_token(sub=f"nonsu-{role}", roles={role: {"1": "d"}})
        user, _ = _authenticate(token)
        assert user.is_superuser is False, role
