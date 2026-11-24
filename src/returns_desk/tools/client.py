"""A synchronous MCP client for the agent workflow.

The MCP SDK is async and the rest of this service is sync, so one background event loop
(an anyio blocking portal) runs the client sessions. Each agent run opens its own session
against a tool server bound to that run's verified principal.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from anyio.from_thread import BlockingPortal, start_blocking_portal
from mcp import Client
from mcp.server.mcpserver import MCPServer


class ToolFailure(Exception):
    """A tool refused or failed. `code` is the server's error code, such as not_found."""

    def __init__(self, tool: str, code: str, message: str) -> None:
        super().__init__(f"{tool}: {code}: {message}")
        self.tool = tool
        self.code = code
        self.message = message


class ToolSession:
    def __init__(self, portal: BlockingPortal, client: Client) -> None:
        self._portal = portal
        self._client = client
        # Names and argument keys of every call, for the trace. Never the results.
        self.calls: list[dict[str, Any]] = []

    def list_tools(self) -> list[str]:
        return [tool.name for tool in self._portal.call(self._client.list_tools).tools]

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        arguments = {k: v for k, v in arguments.items() if v is not None}
        result = self._portal.call(self._client.call_tool, name, arguments)
        if result.is_error:
            code, message = _parse_error(result)
            self.calls.append({"tool": name, "arguments": sorted(arguments), "error": code})
            raise ToolFailure(name, code, message)
        self.calls.append({"tool": name, "arguments": sorted(arguments), "error": None})
        return result.structured_content or {}


def _parse_error(result: Any) -> tuple[str, str]:
    text = " ".join(getattr(block, "text", "") or "" for block in result.content)
    start = text.find("{")
    if start >= 0:
        try:
            payload = json.loads(text[start:])
            return str(payload.get("code", "tool_error")), str(payload.get("message", text))
        except ValueError:
            pass
    return "tool_error", text


class ToolRuntime:
    """Owns the background event loop. One per process."""

    def __init__(self) -> None:
        self._portal_cm = start_blocking_portal()
        self._portal = self._portal_cm.__enter__()

    def close(self) -> None:
        self._portal_cm.__exit__(None, None, None)

    @contextmanager
    def session(self, server: MCPServer) -> Iterator[ToolSession]:
        with self._portal.wrap_async_context_manager(Client(server)) as client:
            yield ToolSession(self._portal, client)
