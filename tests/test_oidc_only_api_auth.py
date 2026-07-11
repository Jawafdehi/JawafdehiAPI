"""Integration tests for the OIDC-only API authentication path (phase5).

These tests assert the *request-level* security guarantee behind the OIDC-only
migration that removed DRF TokenAuthentication / SessionAuthentication / the
SimpleJWT auth from the API:

  (a) the legacy ``Authorization: Token <key>`` scheme is NOT accepted on a
      protected endpoint — OIDCAuthentication ignores the non-Bearer header
      (it is never even parsed as a token), so DRF returns 401 with a *Bearer*
      challenge rather than a TokenAuthentication "Invalid token." response;
  (b) a valid Zitadel OIDC Bearer access token still authenticates and
      authorizes a write (JWKS stubbed in-process, as in tests/test_oidc_auth.py);
  (c) an anonymous request to a protected write endpoint is still rejected 401.

The protected endpoint exercised is POST /api/cases/ (draft creation), which
requires authentication + the ``cases.add_case`` model permission.
"""

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from rest_framework.test import APIClient

from cases.models import CaseType
from jawafdehi_shared.auth import oidc as oidc_auth

User = get_user_model()

ISSUER = "https://auth.test.jawafdehi.org"
AUDIENCE = "123456789"
ROLES_CLAIM = "urn:zitadel:iam:org:project:roles"
CASES_URL = "/api/cases/"

_SIGNING_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

VALID_CASE_PAYLOAD = {"title": "OIDC created case", "case_type": CaseType.CORRUPTION}


class _FakeSigningKey:
    def __init__(self, public_key):
        self.key = public_key


class _FakeJWKClient:
    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token):  # signature parity
        return _FakeSigningKey(self._public_key)


@pytest.fixture(autouse=True)
def _oidc_settings(settings):
    settings.OIDC_ISSUER = ISSUER
    settings.OIDC_AUDIENCE = AUDIENCE
    settings.OIDC_JWKS_URI = f"{ISSUER}/oauth/v2/keys"
    settings.OIDC_ROLES_CLAIM = ROLES_CLAIM
    settings.OIDC_ALGORITHMS = ["RS256"]
    settings.OIDC_LEEWAY = 30
    settings.OIDC_ROLE_TO_GROUP = oidc_auth.DEFAULT_ROLE_TO_GROUP
    oidc_auth.reset_jwks_client()
    oidc_auth._jwks_client = _FakeJWKClient(_SIGNING_KEY.public_key())
    yield
    oidc_auth.reset_jwks_client()


def _make_token(*, sub="oidc-sub-1", roles=None):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "aud": AUDIENCE,
        "iss": ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "email": f"{sub}@example.org",
    }
    if roles is not None:
        payload[ROLES_CLAIM] = roles
    return jwt.encode(payload, _SIGNING_KEY, algorithm="RS256")


# ---------------------------------------------------------------------------
# (a) legacy Token scheme is not recognized
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_legacy_token_scheme_not_accepted_on_protected_endpoint():
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Token deadbeef")
    response = client.post(CASES_URL, data=VALID_CASE_PAYLOAD, format="json")

    # Unknown (non-Bearer) scheme -> not authenticated -> 401.
    assert response.status_code == 401
    # The challenge proves no TokenAuthentication is in the chain: the only
    # authenticator (OIDC) advertises Bearer. A surviving TokenAuthentication
    # would instead parse the header and yield a "Token" challenge / "Invalid
    # token." detail.
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
    assert "Token" not in response.headers["WWW-Authenticate"]


@pytest.mark.django_db
def test_malformed_legacy_token_does_not_leak_invalid_token_detail():
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION="Token not-a-real-token")
    response = client.post(CASES_URL, data=VALID_CASE_PAYLOAD, format="json")

    assert response.status_code == 401
    detail = str(response.data.get("detail", "")).lower()
    # The old TokenAuthentication produced "Invalid token." — assert that the
    # Token scheme is no longer parsed and that specific message is gone.
    assert "invalid token" not in detail


# ---------------------------------------------------------------------------
# (b) valid OIDC Bearer token still works
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_oidc_bearer_token_authorizes_write():
    # Group must exist so OIDC role->group sync can attach it.
    contributor_group, _ = Group.objects.get_or_create(name="Caseworker")
    # DjangoModelPermissions maps POST -> cases.add_case; grant it to the group
    # so the synced user is authorized (mirrors the create_groups ops step).
    contributor_group.permissions.add(Permission.objects.get(codename="add_case"))

    token = _make_token(sub="oidc-writer", roles={"caseworker": {"1": "d"}})
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = client.post(CASES_URL, data=VALID_CASE_PAYLOAD, format="json")

    assert response.status_code == 201, response.data
    # The OIDC sub-keyed user was created and authenticated.
    assert User.objects.filter(username="oidc-writer").exists()


@pytest.mark.django_db
def test_oidc_bearer_without_add_perm_is_forbidden():
    # A ReadOnly principal is authenticated but its group holds only view_*, not
    # cases.add_case, so DjangoModelPermissions (POST -> add_case) rejects it.
    # (Caseworker can no longer stand in for "no add perm": v3 grants it
    # add_case, so it is a legitimate case creator.)
    Group.objects.get_or_create(name="ReadOnly")
    token = _make_token(sub="oidc-noperm", roles={"readonly": {"1": "d"}})
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
    response = client.post(CASES_URL, data=VALID_CASE_PAYLOAD, format="json")

    # Authenticated (so not 401) but lacks cases.add_case -> 403.
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# (c) anonymous still 401 on a protected write
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_anonymous_write_rejected():
    response = APIClient().post(CASES_URL, data=VALID_CASE_PAYLOAD, format="json")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Bearer")
