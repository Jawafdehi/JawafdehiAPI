"""DEV-ONLY session login for the SPA (gated by DEV_AUTH)."""
import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()


@pytest.mark.django_db
def test_dev_login_success_and_me_roundtrip(settings):
    settings.DEV_AUTH = True
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
def test_dev_login_bad_password(settings):
    settings.DEV_AUTH = True
    User.objects.create_user(username="u", password="right")
    c = APIClient()
    r = c.post("/api/casework/auth/dev-login/", {"username": "u", "password": "wrong"}, format="json")
    assert r.status_code == 401


@pytest.mark.django_db
def test_dev_login_hard_404_when_flag_off(settings):
    # The route itself is only mounted when DEV_AUTH is on at import time, but the
    # view ALSO guards at runtime; simulate the flag being off.
    settings.DEV_AUTH = False
    User.objects.create_user(username="admin2", password="admin2")
    c = APIClient()
    r = c.post("/api/casework/auth/dev-login/", {"username": "admin2", "password": "admin2"}, format="json")
    assert r.status_code == 404
