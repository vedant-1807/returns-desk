"""A synchronous MCP client for the agent workflow.

The MCP SDK is async and the rest of this service is sync, so one background event loop
(an anyio blocking portal) runs the client sessions. Each agent run opens its own session
against a tool server bound to that run's verified principal.
"""

from __future__ import annotations

import json
import time
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


def _trace_fields(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """What the trace keeps of a tool result: IDs and outcome codes, never content. Policy
    text, order details and amounts stay out; the IDs point at the rows that hold them."""
    if name == "get_order":
        return {"orders": len(data.get("orders", []))}
    if name == "search_policy":
        return {
            "evidence_ids": [p["passage_id"] for p in data.get("passages", [])],
            "cache": data.get("cache", "none"),
            "policy_found": data.get("policy_found"),
        }
    if name == "quote_resolution":
        return {
            "proposal_id": data.get("proposal_id"),
            "eligible": data.get("eligible"),
            "requires_review": data.get("requires_review"),
            "action": data.get("action"),
            "reason_codes": data.get("reason_codes"),
        }
    if name == "check_inventory":
        return {"in_stock": [v["variant"] for v in data.get("variants", []) if v["in_stock"]]}
    if name == "submit_resolution":
        return {"outcome": data.get("outcome"), "action_id": data.get("action_id")}
    if name == "get_resolution_status":
        return {"state": data.get("state")}
    return {}


class ToolSession:
    def __init__(self, portal: BlockingPortal, client: Client) -> None:
        self._portal = portal
        self._client = client
        self._opened = time.monotonic()
        # One entry per call, for the trace: the tool, the names of its arguments, how long
        # it took, and the IDs and outcome codes of its result. Never argument values or
        # result content.
        self.calls: list[dict[str, Any]] = []

    def list_tools(self) -> list[str]:
        return [tool.name for tool in self._portal.call(self._client.list_tools).tools]

    def call(self, name: str, **arguments: Any) -> dict[str, Any]:
        arguments = {k: v for k, v in arguments.items() if v is not None}
        started = time.monotonic()
        result = self._portal.call(self._client.call_tool, name, arguments)
        entry: dict[str, Any] = {
            "tool": name,
            "arguments": sorted(arguments),
            "error": None,
            # Offset from the start of the turn, and duration, in milliseconds.
            "at_ms": int((started - self._opened) * 1000),
            "ms": int((time.monotonic() - started) * 1000),
        }
        self.calls.append(entry)
        if result.is_error:
            code, message = _parse_error(result)
            entry["error"] = code
            raise ToolFailure(name, code, message)
        data = result.structured_content or {}
        entry["result"] = _trace_fields(name, data)
        return data


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
