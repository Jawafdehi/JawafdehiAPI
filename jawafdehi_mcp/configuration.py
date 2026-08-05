"""Configuration compatibility between the embedded MCP and Django."""

from __future__ import annotations

import os
from typing import Any

_DJANGO_SETTING_NAMES = {
    "OIDC_ISSUER": "OIDC_ISSUER",
    "OIDC_JWKS_URL": "OIDC_JWKS_URI",
    "OIDC_API_AUDIENCE": "OIDC_AUDIENCE",
    "OIDC_OP_USER_ENDPOINT": "OIDC_OP_USER_ENDPOINT",
}


def get_oidc_config(name: str) -> Any | None:
    """Read authoritative Django OIDC settings, with standalone env fallback."""
    setting_name = _DJANGO_SETTING_NAMES.get(name)
    if setting_name is not None:
        try:
            from django.conf import settings
        except ImportError:
            settings = None

        if settings is not None and settings.configured:
            value = getattr(settings, setting_name, None)
            if value not in (None, "", [], ()):
                return value

    value = os.getenv(name)
    if value and value.strip():
        return value.strip()
    return None
