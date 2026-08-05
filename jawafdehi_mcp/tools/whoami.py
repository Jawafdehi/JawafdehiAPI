"""Tool reporting the current authenticated user's identity and roles."""

import json
from typing import Any

from mcp.types import TextContent

from ..identity import current_user_identity
from .base import BaseTool


class GetCurrentUserTool(BaseTool):
    """Return the current user's display name, email, and roles."""

    @property
    def name(self) -> str:
        return "get_current_user"

    @property
    def description(self) -> str:
        return (
            "Return the current authenticated user's identity — display name, "
            "email, and roles. Use this to check who you are acting as and what "
            "permissions you have. Returns an anonymous result when not signed in."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, arguments: dict[str, Any]) -> list[TextContent]:
        identity = current_user_identity.get()
        if identity is None:
            payload: dict[str, Any] = {
                "authenticated": False,
                "message": "No authenticated user; public read-only access.",
            }
        else:
            payload = {
                "authenticated": True,
                "name": identity.get("name"),
                "email": identity.get("email"),
                "roles": identity.get("roles", []),
                "sub": identity.get("sub"),
            }
        return [
            TextContent(
                type="text",
                text=json.dumps(payload, indent=2, ensure_ascii=False),
            )
        ]
