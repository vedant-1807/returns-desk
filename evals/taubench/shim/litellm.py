"""A stand-in for the `litellm` package, for running τ-bench.

τ-bench calls `litellm.completion(...)` for its agent and its simulated user. This module
offers that one function over a plain OpenAI-compatible HTTP API, so the pinned τ-bench
code runs unmodified without installing LiteLLM. Only what τ-bench uses is implemented.

The runner calls `configure()` once per provider before any completion is made.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import httpx2

_providers: dict[str, dict[str, Any]] = {}
_clients: dict[str, httpx2.Client] = {}
_lock = threading.Lock()

# Running totals the runner reads: calls, tokens and seconds, split by who was calling.
stats: dict[str, dict[str, float]] = {
    "agent": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0},
    "user": {"calls": 0, "input_tokens": 0, "output_tokens": 0, "seconds": 0.0},
}


def configure(
    provider: str,
    *,
    base_url: str,
    api_key: str = "",
    timeout_seconds: float = 600.0,
    extra: dict[str, Any] | None = None,
    user_temperature: float | None = None,
) -> None:
    _providers[provider] = {"extra": extra or {}, "user_temperature": user_temperature}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    _clients[provider] = httpx2.Client(
        base_url=base_url.rstrip("/"), headers=headers, timeout=timeout_seconds
    )


class _Message:
    def __init__(self, data: dict[str, Any]) -> None:
        self.role = data.get("role", "assistant")
        self.content = data.get("content")
        self.tool_calls = data.get("tool_calls") or None

    def model_dump(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "role": self.role,
            "tool_calls": self.tool_calls,
            "function_call": None,
        }


class _Choice:
    def __init__(self, data: dict[str, Any]) -> None:
        self.message = _Message(data.get("message") or {})
        self.finish_reason = data.get("finish_reason")


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.choices = [_Choice(choice) for choice in payload.get("choices", [])]
        self.usage = payload.get("usage") or {}
        self.model = payload.get("model")
        # Local and free-plan models: nothing is charged.
        self._hidden_params = {"response_cost": 0.0}


def _clean(message: dict[str, Any]) -> dict[str, Any]:
    """The request form of a message: no null fields, and assistant text never missing."""
    out = {key: value for key, value in message.items() if value is not None}
    if out.get("role") == "assistant" and "content" not in out:
        out["content"] = ""
    return out


def completion(
    *,
    model: str,
    messages: list[dict[str, Any]],
    custom_llm_provider: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    temperature: float | None = None,
    **_: Any,
) -> _Response:
    provider = custom_llm_provider or next(iter(_providers))
    settings = _providers[provider]
    who = "agent" if tools else "user"
    body: dict[str, Any] = {
        "model": model,
        "messages": [_clean(message) for message in messages],
        **settings["extra"],
    }
    if tools:
        body["tools"] = tools
    if temperature is None and who == "user":
        temperature = settings["user_temperature"]
    if temperature is not None:
        body["temperature"] = temperature
    started = time.monotonic()
    response = _clients[provider].post("/chat/completions", json=body)
    response.raise_for_status()
    payload = response.json()
    usage = payload.get("usage") or {}
    with _lock:
        bucket = stats[who]
        bucket["calls"] += 1
        bucket["input_tokens"] += usage.get("prompt_tokens") or 0
        bucket["output_tokens"] += usage.get("completion_tokens") or 0
        bucket["seconds"] += time.monotonic() - started
    return _Response(payload)
