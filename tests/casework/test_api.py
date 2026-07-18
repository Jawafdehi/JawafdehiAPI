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


def test_basic_mode_rejects_non_loopback_base_url():
    with pytest.raises(ValueError):
        CaseworkApi(
            base_url="https://api.jawafdehi.org",
            basic=("abgen", "local-dev-only"),
        )


def test_basic_mode_accepts_loopback_base_url():
    # Must not raise -- basic= against 127.0.0.1/localhost is the whole point
    # of DEV_AUTH local writes.
    CaseworkApi(base_url="http://127.0.0.1:48010", basic=("abgen", "local-dev-only"))
    CaseworkApi(base_url="http://localhost:48010", basic=("abgen", "local-dev-only"))


# ---------------------------------------------------------------------------
# replace_list -- the highest-risk method: the server deletes every join row
# and recreates from exactly what is sent, so a partial list silently
# destroys the omitted rows.
# ---------------------------------------------------------------------------


def test_replace_list_emits_single_replace_op_for_evidence(monkeypatch):
    seen = {}
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "_request", _fake_request_capturing(seen))

    items = [{"material_iri": "https://jawafdehi.org/material/news/1",
              "additional_details": ""}]
    api.replace_list("some-slug", "evidence", items)

    body = json.loads(seen["data"].decode())
    assert body == [{"op": "replace", "path": "/evidence", "value": items}]


def test_replace_list_emits_single_replace_op_for_entities(monkeypatch):
    seen = {}
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "_request", _fake_request_capturing(seen))

    items = [{"entity_iri": "https://jawafdehi.org/entity/nes/1"}]
    api.replace_list("some-slug", "entities", items)

    body = json.loads(seen["data"].decode())
    assert body == [{"op": "replace", "path": "/entities", "value": items}]


def test_replace_list_rejects_non_whole_list_path():
    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    with pytest.raises(ValueError):
        api.replace_list("some-slug", "bigo", [1, 2, 3])


# ---------------------------------------------------------------------------
# get_case -- must hit the detail endpoint (the only one that resolves
# `material` objects on evidence; the list endpoint returns `material: null`)
# and URL-quote the slug.
# ---------------------------------------------------------------------------


def test_get_case_requests_detail_endpoint_and_quotes_slug(monkeypatch):
    seen = {}

    def fake_request(method, url, data=None, headers=None, timeout=None):
        seen.update(method=method, url=url, data=data, headers=headers)

        class R:
            status = 200

            def read(self):
                return b'{"slug": "a slug with?special"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()

    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "_request", fake_request)

    api.get_case("a slug with?special")

    assert seen["method"] == "GET"
    assert seen["url"] == "http://127.0.0.1:48010/api/cases/a%20slug%20with%3Fspecial/"


# ---------------------------------------------------------------------------
# iter_cases -- must follow pagination until a page has no `next`.
# ---------------------------------------------------------------------------


def test_iter_cases_follows_pagination(monkeypatch):
    pages = {
        1: {"results": [{"slug": "case-a"}, {"slug": "case-b"}], "next": "http://x/?page=2"},
        2: {"results": [{"slug": "case-c"}], "next": None},
    }
    seen_pages = []

    def fake_get(path, params=None, timeout=60):
        page = params["page"]
        seen_pages.append(page)
        return pages[page]

    api = CaseworkApi(base_url="http://127.0.0.1:48010", token="t")
    monkeypatch.setattr(api, "get", fake_get)

    cases = list(api.iter_cases())

    assert [c["slug"] for c in cases] == ["case-a", "case-b", "case-c"]
    assert seen_pages == [1, 2]
