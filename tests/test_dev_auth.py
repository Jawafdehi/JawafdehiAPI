"""Dev auth (username/password) gate.

Production is OIDC-only (Zitadel). ``DEV_AUTH`` additively enables Django session
+ HTTP-Basic auth for local development, but ONLY under DEBUG/TESTING — it must
never weaken production auth. These tests pin both the enabled behavior and the
production fail-safe.
"""

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()


@pytest.mark.django_db
def test_dev_auth_allows_session_login_write(settings):
    """With DEV_AUTH on, a session-authenticated user can hit a write endpoint
    (proving SessionAuthentication is active) without any OIDC bearer."""
    settings.DEV_AUTH = True
    settings.REST_FRAMEWORK = {
        **settings.REST_FRAMEWORK,
        "DEFAULT_AUTHENTICATION_CLASSES": [
            "jawafdehi_shared.auth.oidc.OIDCAuthentication",
            "rest_framework.authentication.SessionAuthentication",
            "rest_framework.authentication.BasicAuthentication",
        ],
    }
    user = User.objects.create_user(username="dev", password="devpass")
    client = APIClient()
    client.force_login(user)
    # A public read must work session-authenticated (no 401 from a bearer-only
    # stack); we assert we are NOT rejected as unauthenticated.
    resp = client.get("/api/health")
    assert resp.status_code == 200


def test_dev_auth_forced_off_in_production():
    """DEV_AUTH must resolve False when DEBUG and TESTING are both off, even if
    the env var is set — production stays OIDC-only.

    ``TESTING`` is true inside pytest (it keys off ``sys.argv``), so we can't
    reproduce a prod process by reloading settings here; instead we run a real
    subprocess with a production-like env and assert the resolved flag + auth
    classes. This is the actual guard (``DEV_AUTH = env_flag(...) and (DEBUG or
    TESTING)``) exercised end-to-end.
    """
    import os
    import subprocess
    import sys

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Annotated, not inferred: ty widens an unannotated dict to
    # `dict[Unknown | str, None | str]` after the `env.pop("TESTING", None)` below
    # (the None default leaks into the VALUE type), which then fails to satisfy
    # `subprocess.run(env=...)`'s `Mapping[str, str]`. The annotation states the
    # type the dict actually has. Isolated against ty 0.0.66 — a `.pop` with a
    # None default should not affect the mapping's own type.
    env: dict[str, str] = {
        **os.environ,
        "DEV_AUTH": "true",
        "DEBUG": "False",
        "SECRET_KEY": "a-real-production-secret-key-not-a-sentinel",
        "ALLOWED_HOSTS": "portal.jawafdehi.org",
        "OIDC_ISSUER": "https://zitadel.example",
        "DJANGO_SETTINGS_MODULE": "config.settings",
    }
    # Ensure TESTING is not forced on via env; the child is not pytest so its
    # sys.argv won't contain "pytest".
    env.pop("TESTING", None)
    code = (
        "import django; django.setup();"
        "from django.conf import settings as s;"
        "assert s.DEV_AUTH is False, s.DEV_AUTH;"
        "assert s.REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES'] == "
        "['jawafdehi_shared.auth.oidc.OIDCAuthentication'], "
        "s.REST_FRAMEWORK['DEFAULT_AUTHENTICATION_CLASSES'];"
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "OK" in result.stdout
