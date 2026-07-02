"""DEV-ONLY session login for the SPA (gated by DEV_AUTH).

The routes are always registered; each view hard-404s at runtime unless
DEV_AUTH is on. me/dev-login/dev-logout pin their authenticators explicitly
(DevAwareSessionAuthentication), so the session path works whenever DEV_AUTH is
on regardless of DRF's import-time freezing of the global auth classes — the
test only needs to flip the flag.
"""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()


@pytest.fixture
def enable_dev_auth(settings):
    settings.DEV_AUTH = True
    return settings


@pytest.mark.django_db
def test_dev_login_success_and_me_roundtrip(enable_dev_auth):
    User.objects.create_user(username="admin", password="admin", is_superuser=True, is_staff=True)
    c = APIClient()
    r = c.post("/api/casework/auth/dev-login/", {"username": "admin", "password": "admin"}, format="json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["username"] == "admin"
    assert body["is_admin"] is True
    assert "csrftoken" in body
    # session now carries into a protected read
    me = c.get("/api/casework/auth/me/")
    assert me.status_code == 200
    assert me.json()["username"] == "admin"


@pytest.mark.django_db
def test_dev_login_bad_password(enable_dev_auth):
    User.objects.create_user(username="u", password="right")
    c = APIClient()
    r = c.post("/api/casework/auth/dev-login/", {"username": "u", "password": "wrong"}, format="json")
    assert r.status_code == 401


@pytest.mark.django_db
def test_dev_login_non_object_body_is_400_not_500(enable_dev_auth):
    # A non-object JSON body (list/scalar) must not blow up request.data.get().
    c = APIClient()
    r = c.post("/api/casework/auth/dev-login/", ["not", "an", "object"], format="json")
    assert r.status_code == 400


@pytest.mark.django_db
def test_dev_login_hard_404_when_flag_off(settings):
    # Routes are always mounted, but the view hard-404s at runtime when the flag
    # is off — the boundary that keeps the platform SSO-only in production.
    settings.DEV_AUTH = False
    User.objects.create_user(username="admin2", password="admin2")
    c = APIClient()
    r = c.post("/api/casework/auth/dev-login/", {"username": "admin2", "password": "admin2"}, format="json")
    assert r.status_code == 404
