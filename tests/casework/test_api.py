import base64
import json

import pytest

from casework.common.api import CaseworkApi, build_replace_patch


def test_build_replace_patch_is_a_list_of_ops():
    patch = build_replace_patch("bigo", 10403941)
    assert isinstance(patch, list)
    assert patch == [{"op": "replace", "path": "/bigo", "value": 10403941}]


def test_build_replace_patch_handles_list_paths():
    items = [{"material_iri": "https://jawafdehi.org/material/news/1",
              "additional_details": ""}]
    assert build_replace_patch("evidence", items) == [
        {"op": "replace", "path": "/evidence", "value": items}
    ]


def test_patch_uses_plain_application_json(monkeypatch):
    seen = {}

    def fake_request(method, url, data=None, headers=None, timeout=None):
        seen.update(method=method, url=url, data=data, headers=headers)
        class R:
            status = 200
            def read(self): return b"{}"
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()

    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "_request", fake_request)
    api.patch_field("some-slug", "bigo", 500)

    assert seen["method"] == "PATCH"
    # 415 if this is application/json-patch+json -- no such parser is registered.
    assert seen["headers"]["Content-Type"] == "application/json"
    assert isinstance(json.loads(seen["data"].decode()), list)


# ---------------------------------------------------------------------------
# Auth mode: Bearer (production, default) vs Basic (local DEV_AUTH only).
#
# OIDCAuthentication runs FIRST in DRF's authenticator chain and treats any
# `Bearer` header as its own to authenticate (and 401s it when OIDC_JWKS_URI/
# OIDC_ISSUER are unset, as they are by default under local sqlite dev). DRF's
# additive DEV_AUTH authenticators (SessionAuthentication/BasicAuthentication)
# only ever get a turn for a request that does NOT carry a Bearer header, so a
# local writer must send `Authorization: Basic ...` instead -- never both.
# ---------------------------------------------------------------------------


def _fake_request_capturing(seen):
    def fake_request(method, url, data=None, headers=None, timeout=None):
        seen.update(method=method, url=url, data=data, headers=headers)

        class R:
            status = 200

            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()

    return fake_request


def test_bearer_mode_sends_bearer_header_and_no_basic(monkeypatch):
    seen = {}
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="prod-token")
    monkeypatch.setattr(api, "_request", _fake_request_capturing(seen))

    api.patch_field("some-slug", "bigo", 500)

    auth = seen["headers"]["Authorization"]
    assert auth == "Bearer prod-token"
    assert not auth.startswith("Basic ")


def test_basic_mode_sends_basic_header_and_no_bearer(monkeypatch):
    seen = {}
    api = CaseworkApi(base_url="http://127.0.0.1:48010", basic=("abgen", "local-dev-only"))
    monkeypatch.setattr(api, "_request", _fake_request_capturing(seen))

    api.patch_field("some-slug", "bigo", 500)

    auth = seen["headers"]["Authorization"]
    assert not auth.startswith("Bearer ")
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth.removeprefix("Basic ")).decode()
    assert decoded == "abgen:local-dev-only"


def test_bearer_and_basic_are_mutually_exclusive():
    with pytest.raises(ValueError):
        CaseworkApi(
            base_url="http://127.0.0.1:48010",
            token="prod-token",
            basic=("abgen", "local-dev-only"),
        )


def test_bearer_or_basic_is_required():
    with pytest.raises(ValueError):
        CaseworkApi(base_url="http://127.0.0.1:48010")
