# tests/conftest.py
import pytest
from paperflow.core.tool import Tool, ToolResult


class MockEchoTool(Tool):
    name = "echo"
    description = "Echo back the input message"
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The message to echo"}
        },
        "required": ["message"],
    }

    def execute(self, message: str) -> ToolResult:
        return ToolResult(text=f"Echo: {message}")
