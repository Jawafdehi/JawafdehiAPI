"""Base tool interface for MCP tools."""

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from mcp.types import TextContent, Tool


class ToolExecutionResult(list[TextContent]):
    """List-compatible tool content with an explicit protocol outcome."""

    def __init__(
        self,
        content: Iterable[TextContent] = (),
        *,
        is_error: bool = False,
    ) -> None:
        super().__init__(content)
        self.is_error = is_error


def error_text(message: str) -> ToolExecutionResult:
    """Return text content that the MCP transport must mark as an error."""
    return ToolExecutionResult(
        [TextContent(type="text", text=message)],
        is_error=True,
    )


class BaseTool(ABC):
    """Base class for MCP tools."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Return the tool name."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Return the tool description."""
        pass

    @property
    @abstractmethod
    def input_schema(self) -> dict[str, Any]:
        """Return the JSON schema for tool input."""
        pass

    def to_tool(self) -> Tool:
        """Convert to MCP Tool object."""
        return Tool(
            name=self.name,
            description=self.description,
            inputSchema=self.input_schema,
        )

    @abstractmethod
    async def execute(
        self, arguments: dict[str, Any]
    ) -> list[TextContent] | ToolExecutionResult:
        """
        Execute the tool with given arguments.

        Args:
            arguments: Tool input arguments

        Returns:
            List of TextContent responses
        """
        pass
