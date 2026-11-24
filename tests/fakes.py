"""Stand-ins for the outside world that still exercise the real client code."""

from __future__ import annotations

import json
from typing import Any

import httpx2

from returns_desk.models import keyword
from returns_desk.models.gateway import (
    ModelRequest,
    ModelResult,
    Route,
    RoutedGateway,
    TurnBudget,
    Validator,
)


class ModelUpstreams:
    """Plays the model providers behind the real HTTP gateway.

    Each route gets a behaviour, or a list of behaviours consumed one call at a time (the
    last one repeats):

        ok            answer the way the keyword stand-in would
        timeout       no answer before the deadline
        error         HTTP 503
        rate_limited  HTTP 429 with no wait time

    So failover, budgets and attempt records run through the production gateway code, and
    only the far end of the HTTP call is scripted.
    """

    def __init__(self, **behaviour: str | list[str]) -> None:
        self._behaviour = {
            name: list(steps) if isinstance(steps, list) else [steps]
            for name, steps in behaviour.items()
        }
        self.calls: list[tuple[str, str]] = []
        self.current: ModelRequest | None = None
        # Per model name, a function that writes that model's answer instead of the
        # keyword stand-in. Lets a test make one tier wrong and the other right.
        self.answer_as: dict[str, Any] = {}
        self.models_called: list[str] = []

    def set(self, **behaviour: str | list[str]) -> None:
        for name, steps in behaviour.items():
            self._behaviour[name] = list(steps) if isinstance(steps, list) else [steps]

    def gateway(
        self,
        *,
        attempts_per_turn: int = 2,
        deadline_seconds: float = 10.0,
        strong_models: bool = False,
    ) -> RoutedGateway:
        upstream = self

        class Gateway(RoutedGateway):
            def complete(
                self, request: ModelRequest, budget: TurnBudget, validate: Validator
            ) -> ModelResult:
                # The fake upstream answers from the same facts the prompt was built from.
                upstream.current = request
                return super().complete(request, budget, validate)

        return Gateway(
            [
                Route(
                    name,
                    f"https://{name}.test/v1",
                    f"key-{name}",
                    f"{name}-low",
                    f"{name}-strong" if strong_models else f"{name}-low",
                )
                for name in self._behaviour
            ],
            attempts_per_turn=attempts_per_turn,
            deadline_seconds=deadline_seconds,
            transport=httpx2.MockTransport(self._handle),
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        route = request.url.host.split(".")[0]
        steps = self._behaviour[route]
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        self.calls.append((route, step))
        if step == "timeout":
            raise httpx2.ReadTimeout("scripted timeout", request=request)
        if step == "error":
            return httpx2.Response(503, json={"error": {"message": "scripted outage"}})
        if step == "rate_limited":
            return httpx2.Response(429, json={"error": {"message": "scripted rate limit"}})
        assert step == "ok", f"unknown scripted behaviour {step!r}"
        assert self.current is not None
        body: dict[str, Any] = json.loads(request.content)
        self.models_called.append(body["model"])
        write = self.answer_as.get(body["model"], keyword.handle)
        return httpx2.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": json.dumps(write(self.current))}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20},
            },
        )
