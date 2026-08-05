"""Tests for OIDC bearer-token verification and identity resolution."""

import time
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from jawafdehi_shared.auth import oidc as platform_oidc
from jawafdehi_mcp import oidc

ISSUER = "https://auth.test.invalid"
AUDIENCE = "test-project-id"


@pytest.fixture(autouse=True)
def _oidc_settings(settings):
    settings.OIDC_ISSUER = ISSUER
    settings.OIDC_AUDIENCE = AUDIENCE
    settings.OIDC_JWKS_URI = "https://auth.test.invalid/keys"
    settings.OIDC_OP_USER_ENDPOINT = "https://auth.test.invalid/userinfo"
    settings.OIDC_ALGORITHMS = ["RS256"]
    settings.OIDC_LEEWAY = 30
    # Reset module caches between tests.
    platform_oidc.reset_jwks_client()
    oidc._userinfo_cache.clear()
    yield
    platform_oidc.reset_jwks_client()


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(autouse=True)
def _fake_jwks(monkeypatch, rsa_key):
    """Make verify use our test key instead of fetching a real JWKS."""

    class _SigningKey:
        key = rsa_key.public_key()
        key_id = "test-key"

    class _FakeClient:
        def get_signing_keys(self, refresh=False):
            return [_SigningKey()]

        @staticmethod
        def match_kid(signing_keys, kid):
            return next(
                (key for key in signing_keys if key.key_id == kid),
                None,
            )

    monkeypatch.setattr(platform_oidc, "_get_jwks_client", lambda: _FakeClient())


def _mint(rsa_key, *, missing=(), headers=None, **overrides):
    now = int(time.time())
    payload = {
        "sub": "user-123",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "exp": now + 300,
        "iat": now,
        "jti": "jti-1",
    }
    payload.update(overrides)
    for claim in missing:
        payload.pop(claim, None)
    return jwt.encode(
        payload,
        rsa_key,
        algorithm="RS256",
        headers=headers or {"kid": "test-key"},
    )


class TestVerifyBearerToken:
    def test_valid_token(self, rsa_key):
        claims = oidc.verify_bearer_token(_mint(rsa_key))
        assert claims["sub"] == "user-123"

    def test_jwe_rejected(self):
        with pytest.raises(oidc.OIDCError):
            oidc.verify_bearer_token("a.b.c.d.e")

    def test_wrong_audience(self, rsa_key):
        with pytest.raises(oidc.OIDCError):
            oidc.verify_bearer_token(_mint(rsa_key, aud="other"))

    def test_wrong_issuer(self, rsa_key):
        with pytest.raises(oidc.OIDCError):
            oidc.verify_bearer_token(_mint(rsa_key, iss="https://evil.invalid"))

    def test_expired(self, rsa_key):
        # Beyond the clock-skew leeway (_CLOCK_SKEW_LEEWAY) so it's unambiguously
        # expired.
        with pytest.raises(oidc.OIDCError):
            oidc.verify_bearer_token(_mint(rsa_key, exp=int(time.time()) - 3600))

    def test_expired_within_clock_skew_leeway_is_allowed(self, rsa_key):
        # A token just past exp (within leeway) is tolerated for clock drift.
        skew = 25
        claims = oidc.verify_bearer_token(_mint(rsa_key, exp=int(time.time()) - skew))
        assert claims["sub"] == "user-123"

    @pytest.mark.parametrize("claim", ["iat", "sub"])
    def test_requires_same_identity_claims_as_django(self, rsa_key, claim):
        with pytest.raises(oidc.OIDCError):
            oidc.verify_bearer_token(_mint(rsa_key, missing=(claim,)))

    def test_missing_issuer_config(self, rsa_key, settings):
        settings.OIDC_ISSUER = ""
        with pytest.raises(oidc.OIDCError):
            oidc.verify_bearer_token(_mint(rsa_key))


class TestSigningKeyRefetch:
    """The kid-miss JWKS refetch is rate-limited so forged/unknown kids can't
    force a fetch per request (DoS / cache-bust)."""

    def test_refetch_is_rate_limited(self, monkeypatch, rsa_key):
        calls = []

        class _AlwaysMiss:
            def get_signing_keys(self, refresh=False):
                calls.append(refresh)
                return []

            @staticmethod
            def match_kid(signing_keys, kid):
                return None

        monkeypatch.setattr(platform_oidc, "_get_jwks_client", lambda: _AlwaysMiss())
        platform_oidc._jwks_last_refresh = 0.0
        token = _mint(rsa_key, headers={"kid": "unknown"})

        # Two cached reads per miss: one outside _jwks_lock (kept out of it so a
        # slow JWKS endpoint cannot serialise every authentication) and one
        # re-check inside it, so a kid a concurrent refresh just landed is found
        # instead of being rejected by the rate limit below.
        with pytest.raises(jwt.exceptions.PyJWKClientError):
            platform_oidc._signing_key_for(token)
        assert calls == [False, False, True]

        # An immediate second miss checks the cache but cannot force network I/O.
        with pytest.raises(jwt.exceptions.PyJWKClientError):
            platform_oidc._signing_key_for(token)
        assert calls == [False, False, True, False, False]
        # The property that matters: exactly one forced refresh across both.
        assert calls.count(True) == 1

    def test_refetch_allowed_after_interval(self, monkeypatch, rsa_key):
        calls = []

        class _AlwaysMiss:
            def get_signing_keys(self, refresh=False):
                calls.append(refresh)
                return []

            @staticmethod
            def match_kid(signing_keys, kid):
                return None

        monkeypatch.setattr(platform_oidc, "_get_jwks_client", lambda: _AlwaysMiss())
        # Pretend the last refetch was long enough ago.
        platform_oidc._jwks_last_refresh = (
            time.monotonic() - platform_oidc._JWKS_MIN_REFRESH_INTERVAL - 1
        )
        with pytest.raises(jwt.exceptions.PyJWKClientError):
            platform_oidc._signing_key_for(
                _mint(rsa_key, headers={"kid": "still-unknown"})
            )
        assert calls == [False, False, True]


class TestBuildIdentity:
    def test_builds_from_userinfo(self):
        claims = {"sub": "abc"}
        info = {
            "email": "Jane@Example.ORG",
            "name": "Jane Doe",
            "roles": ["contributor", "staff"],
        }
        identity = oidc.build_identity(claims, info)
        assert identity == {
            "sub": "abc",
            "email": "jane@example.org",
            "name": "Jane Doe",
            "roles": ["contributor", "staff"],
        }

    def test_name_falls_back_to_given_family(self):
        identity = oidc.build_identity(
            {"sub": "x"},
            {"given_name": "Ram", "family_name": "Sharma", "email": "r@x.org"},
        )
        assert identity["name"] == "Ram Sharma"

    def test_non_list_roles_become_empty(self):
        identity = oidc.build_identity({"sub": "x"}, {"roles": "contributor"})
        assert identity["roles"] == []


class TestDevelopmentIdentity:
    def test_requires_both_testing_and_dev_auth(self, settings):
        settings.TESTING = True
        settings.DEV_AUTH = False
        settings.DEV_NGM_QUERY_TOKEN = "e2e-token"

        assert oidc._development_identity("e2e-token") is None

        settings.TESTING = False
        settings.DEV_AUTH = True
        assert oidc._development_identity("e2e-token") is None

    def test_rejects_a_different_token(self, settings):
        settings.TESTING = True
        settings.DEV_AUTH = True
        settings.DEV_NGM_QUERY_TOKEN = "e2e-token"

        assert oidc._development_identity("wrong-token") is None

    @pytest.mark.asyncio
    async def test_resolves_guarded_e2e_token_without_jwks(
        self, monkeypatch, settings
    ):
        settings.TESTING = True
        settings.DEV_AUTH = True
        settings.DEV_NGM_QUERY_TOKEN = "e2e-token"
        settings.DEV_NGM_QUERY_USERNAME = "mcp-query-e2e"
        verify = MagicMock(side_effect=AssertionError("JWKS must not be used"))
        monkeypatch.setattr(oidc, "verify_bearer_token", verify)

        identity = await oidc.resolve_bearer_identity("e2e-token")

        assert identity == {
            "sub": "mcp-query-e2e",
            "email": None,
            "name": "mcp-query-e2e",
            "roles": [],
        }
        verify.assert_not_called()


@pytest.mark.asyncio
class TestFetchUserinfo:
    async def test_caches_per_token(self, monkeypatch):
        calls = {"n": 0}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"email": "a@x.org", "roles": ["admin"]}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                calls["n"] += 1
                return _Resp()

        monkeypatch.setattr(oidc.httpx, "AsyncClient", lambda *a, **k: _Client())

        claims = {"jti": "t1", "exp": time.time() + 300}
        first = await oidc.fetch_userinfo("tok", claims)
        second = await oidc.fetch_userinfo("tok", claims)
        assert first == second
        assert calls["n"] == 1

    async def test_cache_key_is_token_specific_without_jti(self, monkeypatch):
        calls = {"n": 0}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"email": "a@x.org"}

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                calls["n"] += 1
                return _Resp()

        monkeypatch.setattr(oidc.httpx, "AsyncClient", lambda *a, **k: _Client())
        claims = {"sub": "same-user", "exp": time.time() + 300}

        await oidc.fetch_userinfo("token-a", claims)
        await oidc.fetch_userinfo("token-b", claims)

        assert calls["n"] == 2

    async def test_verified_token_survives_userinfo_failure(self, monkeypatch):
        claims = {
            "sub": "machine-user",
            "email": "machine@example.org",
            "roles": ["contributor"],
        }
        monkeypatch.setattr(oidc, "verify_bearer_token", lambda token: claims)
        monkeypatch.setattr(
            oidc,
            "fetch_userinfo",
            AsyncMock(side_effect=oidc.OIDCError("userinfo unavailable")),
        )

        identity = await oidc.resolve_bearer_identity("verified-token")

        assert identity == {
            "sub": "machine-user",
            "email": "machine@example.org",
            "name": None,
            "roles": ["contributor"],
        }
