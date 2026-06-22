"""Tests for PublicCacheHeadersMiddleware (config/middleware.py).

Pure unit tests: a RequestFactory request plus a fake get_response, no database.
The middleware reads its settings at __init__, so each test configures the
``settings`` fixture first and then builds the middleware.
"""

import pytest
from django.conf import settings as django_settings
from django.http import HttpResponse
from django.test import RequestFactory

from config.middleware import PublicCacheHeadersMiddleware

PATHS = ("/api/statistics/", "/api/cases/")

rf = RequestFactory()


@pytest.fixture
def cache_settings(settings):
    settings.PUBLIC_CACHE_ENABLED = True
    settings.PUBLIC_CACHE_SMAXAGE = 300
    settings.PUBLIC_CACHE_MAXAGE = 300
    settings.PUBLIC_CACHE_PATHS = PATHS
    return settings


def _mw(response):
    return PublicCacheHeadersMiddleware(lambda request: response)


@pytest.mark.usefixtures("cache_settings")
class TestPublicCacheHeaders:
    def test_anon_get_allowlisted_is_cacheable(self):
        resp = HttpResponse("ok")
        resp["Vary"] = "Cookie, Origin"
        out = _mw(resp)(rf.get("/api/statistics/"))

        cc = out["Cache-Control"]
        assert "public" in cc
        assert "s-maxage=300" in cc
        assert "max-age=300" in cc
        assert "cookie" not in out["Vary"].lower()
        assert "Origin" in out["Vary"]

    def test_vary_header_dropped_when_only_cookie(self):
        resp = HttpResponse("ok")
        resp["Vary"] = "Cookie"
        out = _mw(resp)(rf.get("/api/cases/"))
        assert not out.has_header("Vary")

    def test_authorization_header_bypasses(self):
        resp = HttpResponse("ok")
        resp["Vary"] = "Cookie"
        out = _mw(resp)(rf.get("/api/statistics/", HTTP_AUTHORIZATION="Bearer x"))
        assert not out.has_header("Cache-Control")
        assert out["Vary"] == "Cookie"

    def test_session_cookie_bypasses(self):
        resp = HttpResponse("ok")
        resp["Vary"] = "Cookie"
        request = rf.get("/api/statistics/")
        request.COOKIES[django_settings.SESSION_COOKIE_NAME] = "abc123"
        out = _mw(resp)(request)
        assert not out.has_header("Cache-Control")
        assert out["Vary"] == "Cookie"

    def test_authenticated_user_bypasses(self):
        class FakeUser:
            is_authenticated = True

        resp = HttpResponse("ok")
        resp["Vary"] = "Cookie"
        request = rf.get("/api/statistics/")
        request.user = FakeUser()
        out = _mw(resp)(request)
        assert not out.has_header("Cache-Control")
        assert out["Vary"] == "Cookie"

    def test_non_allowlisted_path_untouched(self):
        resp = HttpResponse("ok")
        resp["Vary"] = "Cookie"
        out = _mw(resp)(rf.get("/api/search/"))
        assert not out.has_header("Cache-Control")
        assert out["Vary"] == "Cookie"

    def test_non_get_method_untouched(self):
        resp = HttpResponse("ok")
        out = _mw(resp)(rf.post("/api/statistics/"))
        assert not out.has_header("Cache-Control")

    def test_non_200_untouched(self):
        resp = HttpResponse("nope", status=404)
        out = _mw(resp)(rf.get("/api/statistics/"))
        assert not out.has_header("Cache-Control")

    def test_response_with_set_cookie_untouched(self):
        resp = HttpResponse("ok")
        resp.set_cookie("foo", "bar")
        out = _mw(resp)(rf.get("/api/statistics/"))
        assert not out.has_header("Cache-Control")

    def test_disabled_is_noop(self, cache_settings):
        cache_settings.PUBLIC_CACHE_ENABLED = False
        resp = HttpResponse("ok")
        resp["Vary"] = "Cookie"
        out = _mw(resp)(rf.get("/api/statistics/"))
        assert not out.has_header("Cache-Control")
        assert out["Vary"] == "Cookie"
