"""Tests for the get_current_user tool."""

import json

import pytest

from jawafdehi_mcp.identity import current_user_identity
from jawafdehi_mcp.tools.whoami import GetCurrentUserTool


@pytest.mark.asyncio
class TestGetCurrentUser:
    async def test_anonymous(self):
        token = current_user_identity.set(None)
        try:
            result = await GetCurrentUserTool().execute({})
        finally:
            current_user_identity.reset(token)
        payload = json.loads(result[0].text)
        assert payload["authenticated"] is False

    async def test_authenticated(self):
        identity = {
            "sub": "u1",
            "email": "jane@x.org",
            "name": "Jane",
            "roles": ["admin"],
        }
        token = current_user_identity.set(identity)
        try:
            result = await GetCurrentUserTool().execute({})
        finally:
            current_user_identity.reset(token)
        payload = json.loads(result[0].text)
        assert payload == {
            "authenticated": True,
            "name": "Jane",
            "email": "jane@x.org",
            "roles": ["admin"],
            "sub": "u1",
        }
