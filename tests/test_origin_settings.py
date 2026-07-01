import pytest
from django.core.checks.compatibility.django_4_0 import check_csrf_trusted_origins

from config.settings import get_env_list


def test_get_env_list_parses_regex_values(monkeypatch):
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGIN_REGEXES",
        "^https://[a-zA-Z0-9-]+\\.newnepal\\.workers\\.dev$",
    )

    assert get_env_list("CORS_ALLOWED_ORIGIN_REGEXES") == [
        "^https://[a-zA-Z0-9-]+\\.newnepal\\.workers\\.dev$",
    ]


@pytest.mark.parametrize(
    "origin",
    ["https://*.newnepal.workers.dev", "https://portal.jawafdehi.org"],
)
def test_csrf_trusted_origins_accept_expected_origin_formats(settings, origin):
    settings.CSRF_TRUSTED_ORIGINS = [origin]

    assert check_csrf_trusted_origins(None) == []
