"""The model gateway: one internal interface for every model call.

Every upstream (local Ollama, Groq, OpenRouter) speaks the OpenAI-compatible chat API, so
a single small client covers all routes. The gateway adds what the rest of the service
relies on:

* a per-turn budget (attempts and one combined deadline) shared by retries and failover,
* failover to the next route on a timeout, a rate limit or a server error,
* for batch jobs only, waiting out a rate limit when the upstream says how long (free
  plans allow a few thousand tokens a minute, and an eval has no reason to fail over),
* strict JSON output checked by the caller's validator, with an invalid answer counted as
  a failed attempt,
* a record of every attempt: upstream service, the model it reported, tokens, latency.

The model is never given tools. It reads text and fills in a form; code does the rest.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx2

from returns_desk.config import Settings

Tier = Literal["low", "strong"]
Purpose = Literal["interpret", "answer", "judge", "paraphrase"]
Router = Literal["routed", "always_strong", "always_low"]


@dataclass(frozen=True)
class ModelRequest:
    purpose: Purpose
    system: str
    user: str
    schema_name: str
    schema: dict[str, Any]
    tier: Tier = "low"
    max_output_tokens: int = 500
    # The same facts the prompt was rendered from, for offline stand-ins and for tests.
    inputs: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Attempt:
    route: str
    model: str
    tier: Tier
    purpose: Purpose
    outcome: str  # ok | invalid_output | timeout | rate_limited | error | budget_exhausted
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_ms: int | None = None
    error: str | None = None


@dataclass
class ModelResult:
    data: dict[str, Any] | None
    attempts: list[Attempt]

    @property
    def ok(self) -> bool:
        return self.data is not None


class TurnBudget:
    """How much model work one shopper turn may use. Escalation and failover share it."""

    def __init__(self, attempts: int, deadline_seconds: float) -> None:
        self.attempts_left = attempts
        self._deadline = time.monotonic() + deadline_seconds
        # Why no model may be called at all this turn, such as "daily_token_cap".
        self.blocked_reason: str | None = None

    def block(self, reason: str) -> None:
        """Close the budget for the whole turn. Every later `take` returns None, so no
        model is called and the agent falls back to rules, quoted text or a hand-off."""
        self.blocked_reason = reason

    def remaining_seconds(self) -> float:
        return self._deadline - time.monotonic()

    def take(self) -> float | None:
        """Claim one attempt. Returns the seconds it may run, or None when the budget is
        spent."""
        remaining = self.remaining_seconds()
        if self.blocked_reason or self.attempts_left <= 0 or remaining <= 0.05:
            return None
        self.attempts_left -= 1
        return remaining

    def give_back(self, waited_seconds: float) -> None:
        """Undo one `take` for a request the upstream turned away before any model ran, and
        keep the wait it asked for out of the deadline."""
        self.attempts_left += 1
        self._deadline += waited_seconds


Validator = Callable[[dict[str, Any]], Any]


class ModelGateway(Protocol):
    def new_budget(self) -> TurnBudget: ...

    def complete(
        self, request: ModelRequest, budget: TurnBudget, validate: Validator
    ) -> ModelResult: ...

    def has_distinct_strong_tier(self) -> bool: ...


class _RouteError(Exception):
    def __init__(self, outcome: str, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.outcome = outcome
        # Seconds the upstream asked us to wait. Only a rate limit carries this.
        self.retry_after = retry_after


# One call waits out at most this many rate limits before it gives up on the route.
_MAX_WAITS_PER_CALL = 4
# Added to the wait the upstream asked for, so the retry does not land a moment too early.
_WAIT_PADDING_SECONDS = 0.25


@dataclass
class Route:
    """One upstream service and the two models it serves."""

    name: str
    base_url: str
    api_key: str
    model_low: str
    model_strong: str
    # Extra request fields this upstream needs.
    extra: dict[str, Any] = field(default_factory=dict)

    def model_for(self, tier: Tier) -> str:
        return self.model_strong if tier == "strong" else self.model_low


class RoutedGateway:
    def __init__(
        self,
        routes: list[Route],
        *,
        attempts_per_turn: int,
        deadline_seconds: float,
        rate_limit_wait_seconds: float = 0.0,
        router: Router = "routed",
        sleep: Callable[[float], None] = time.sleep,
        transport: httpx2.BaseTransport | None = None,
    ):
        if not routes:
            raise RuntimeError("No model routes configured. Set RD_MODEL_ROUTES.")
        if router not in ("routed", "always_strong", "always_low"):
            raise RuntimeError(
                f"unknown model router {router!r}; use routed, always_strong or always_low"
            )
        self._routes = routes
        self._router: Router = router
        self._attempts_per_turn = attempts_per_turn
        self._deadline_seconds = deadline_seconds
        # The longest single wait this gateway accepts from a rate-limited upstream.
        # Zero, the default, means never wait: a live chat fails over at once.
        self._rate_limit_wait_seconds = rate_limit_wait_seconds
        self._sleep = sleep
        self._stats_lock = threading.Lock()
        self.rate_limit_waits = 0
        self.waited_seconds = 0.0
        self._clients = {
            route.name: httpx2.Client(
                base_url=route.base_url.rstrip("/"),
                headers={"Authorization": f"Bearer {route.api_key}"} if route.api_key else {},
                transport=transport,
            )
            for route in routes
        }

    def close(self) -> None:
        for client in self._clients.values():
            client.close()

    def new_budget(self) -> TurnBudget:
        return TurnBudget(self._attempts_per_turn, self._deadline_seconds)

    def has_distinct_strong_tier(self) -> bool:
        """True when escalating to the stronger tier can change anything. Under a fixed
        router every call already uses one tier, so there is nothing to escalate to."""
        if self._router != "routed":
            return False
        return any(route.model_low != route.model_strong for route in self._routes)

    def _tier(self, request: ModelRequest) -> Tier:
        if self._router == "always_strong":
            return "strong"
        if self._router == "always_low":
            return "low"
        return request.tier

    def complete(
        self, request: ModelRequest, budget: TurnBudget, validate: Validator
    ) -> ModelResult:
        attempts: list[Attempt] = []
        route_index = 0
        waits = 0
        tier = self._tier(request)
        while True:
            seconds = budget.take()
            route = self._routes[min(route_index, len(self._routes) - 1)]
            model = route.model_for(tier)
            if seconds is None:
                attempts.append(
                    Attempt(
                        route.name,
                        model,
                        tier,
                        request.purpose,
                        "budget_exhausted",
                        error=budget.blocked_reason,
                    )
                )
                return ModelResult(None, attempts)

            started = time.monotonic()
            try:
                data, reported_model, usage = self._call(route, model, request, seconds)
                validate(data)
            except _RouteError as exc:
                wait = self._wait_for(exc, waits)
                attempts.append(
                    Attempt(
                        route.name,
                        model,
                        tier,
                        request.purpose,
                        exc.outcome,
                        latency_ms=_ms(started),
                        error=(str(exc) + (f"; waited {wait:.1f}s" if wait else ""))[:300],
                    )
                )
                if wait is not None:
                    # The upstream said when it will take the request. No model ran, so
                    # the attempt is not spent: wait, then ask the same route again.
                    waits += 1
                    self._sleep(wait)
                    budget.give_back(wait)
                    with self._stats_lock:
                        self.rate_limit_waits += 1
                        self.waited_seconds += wait
                    continue
                # Transport trouble on this route: try the next one, if there is one.
                route_index += 1
                if route_index >= len(self._routes):
                    route_index = 0
                continue
            except Exception as exc:  # noqa: BLE001 - the validator rejected the answer
                attempts.append(
                    Attempt(
                        route.name,
                        model,
                        tier,
                        request.purpose,
                        "invalid_output",
                        latency_ms=_ms(started),
                        error=str(exc)[:300],
                    )
                )
                continue
            attempts.append(
                Attempt(
                    route.name,
                    reported_model or model,
                    tier,
                    request.purpose,
                    "ok",
                    input_tokens=usage.get("prompt_tokens"),
                    output_tokens=usage.get("completion_tokens"),
                    latency_ms=_ms(started),
                )
            )
            return ModelResult(data, attempts)

    def _wait_for(self, exc: _RouteError, waits_so_far: int) -> float | None:
        """Seconds to wait before asking the same route again, or None to fail over."""
        if exc.outcome != "rate_limited" or exc.retry_after is None:
            return None
        if waits_so_far >= _MAX_WAITS_PER_CALL:
            return None
        wait = exc.retry_after + _WAIT_PADDING_SECONDS
        return wait if wait <= self._rate_limit_wait_seconds else None

    def _call(
        self, route: Route, model: str, request: ModelRequest, timeout: float
    ) -> tuple[dict[str, Any], str | None, dict[str, Any]]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.schema,
                },
            },
            "temperature": 0,
            "max_tokens": request.max_output_tokens,
            **route.extra,
        }
        try:
            response = self._clients[route.name].post(
                "/chat/completions", json=body, timeout=timeout
            )
        except httpx2.TimeoutException as exc:
            raise _RouteError("timeout", f"timed out after {timeout:.1f}s") from exc
        except httpx2.HTTPError as exc:
            raise _RouteError("error", f"connection failed: {exc!r}") from exc
        if response.status_code == 429:
            raise _RouteError("rate_limited", "rate limited (429)", _retry_after(response))
        if response.status_code >= 500:
            raise _RouteError("error", f"upstream error {response.status_code}")
        if response.status_code >= 400:
            raise _RouteError("error", f"rejected {response.status_code}: {response.text[:200]}")
        payload = response.json()
        try:
            content = payload["choices"][0]["message"]["content"]
            data = json.loads(content)
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            # Not JSON at all: let the caller's validator path count it as invalid output.
            raise ValueError(f"model did not return JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("model returned JSON that is not an object")
        return data, payload.get("model"), payload.get("usage") or {}


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _retry_after(response: httpx2.Response) -> float | None:
    """The seconds a rate-limited upstream asked us to wait, if it gave a number."""
    try:
        return max(0.0, float(response.headers.get("retry-after", "")))
    except ValueError:
        return None


class ScriptedGateway:
    """A stand-in gateway driven by a Python function. Tests use it to make the model say
    exactly what a scenario needs, including wrong, hostile or failing answers."""

    def __init__(
        self,
        handler: Callable[[ModelRequest], dict[str, Any] | Exception],
        *,
        attempts_per_turn: int = 2,
        deadline_seconds: float = 30.0,
        name: str = "scripted",
    ) -> None:
        self.handler = handler
        self.requests: list[ModelRequest] = []
        self._attempts_per_turn = attempts_per_turn
        self._deadline_seconds = deadline_seconds
        self._name = name

    def close(self) -> None:
        return None

    def new_budget(self) -> TurnBudget:
        return TurnBudget(self._attempts_per_turn, self._deadline_seconds)

    def has_distinct_strong_tier(self) -> bool:
        return False

    def complete(
        self, request: ModelRequest, budget: TurnBudget, validate: Validator
    ) -> ModelResult:
        attempts: list[Attempt] = []
        while True:
            if budget.take() is None:
                attempts.append(
                    Attempt(
                        self._name,
                        self._name,
                        request.tier,
                        request.purpose,
                        "budget_exhausted",
                        error=budget.blocked_reason,
                    )
                )
                return ModelResult(None, attempts)
            self.requests.append(request)
            answer = self.handler(request)
            if isinstance(answer, TimeoutError):
                attempts.append(
                    Attempt(self._name, self._name, request.tier, request.purpose, "timeout")
                )
                continue
            if isinstance(answer, Exception):
                attempts.append(
                    Attempt(
                        self._name,
                        self._name,
                        request.tier,
                        request.purpose,
                        "error",
                        error=str(answer)[:300],
                    )
                )
                continue
            try:
                validate(answer)
            except Exception as exc:  # noqa: BLE001
                attempts.append(
                    Attempt(
                        self._name,
                        self._name,
                        request.tier,
                        request.purpose,
                        "invalid_output",
                        error=str(exc)[:300],
                    )
                )
                continue
            attempts.append(
                Attempt(
                    self._name,
                    self._name,
                    request.tier,
                    request.purpose,
                    "ok",
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=0,
                )
            )
            return ModelResult(answer, attempts)


def build_gateway(settings: Settings, clock: Any = None) -> ModelGateway:
    routes: list[Route] = []
    for name in [part.strip() for part in settings.model_routes.split(",") if part.strip()]:
        if name == "keyword":
            from returns_desk.models.keyword import keyword_gateway

            return keyword_gateway(settings)
        if name == "ollama":
            routes.append(
                Route(
                    "ollama",
                    settings.ollama_base_url,
                    "",
                    settings.ollama_model_low,
                    settings.ollama_model_strong,
                )
            )
        elif name == "groq":
            if not settings.groq_api_key:
                raise RuntimeError("The groq route needs RD_GROQ_API_KEY")
            routes.append(
                Route(
                    "groq",
                    settings.groq_base_url,
                    settings.groq_api_key,
                    settings.groq_model_low,
                    settings.groq_model_strong,
                    # gpt-oss models reason before answering; keep that short and cheap.
                    extra={"reasoning_effort": "low"},
                )
            )
        elif name == "groq_alt":
            if not settings.groq_api_key:
                raise RuntimeError("The groq_alt route needs RD_GROQ_API_KEY")
            routes.append(
                Route(
                    "groq_alt",
                    settings.groq_base_url,
                    settings.groq_api_key,
                    settings.groq_alt_model_low,
                    settings.groq_alt_model_strong,
                )
            )
        elif name == "openrouter":
            if not settings.openrouter_api_key:
                raise RuntimeError("The openrouter route needs RD_OPENROUTER_API_KEY")
            routes.append(
                Route(
                    "openrouter",
                    settings.openrouter_base_url,
                    settings.openrouter_api_key,
                    settings.openrouter_model_low,
                    settings.openrouter_model_strong,
                    # Only use providers that honor strict JSON schema output.
                    extra={"provider": {"require_parameters": True}},
                )
            )
        else:
            raise RuntimeError(
                f"unknown model route {name!r}; use ollama, groq, groq_alt, openrouter or keyword"
            )
    return RoutedGateway(
        routes,
        attempts_per_turn=settings.model_attempts_per_turn,
        deadline_seconds=settings.model_deadline_seconds,
        rate_limit_wait_seconds=settings.model_rate_limit_wait_seconds,
        router=settings.model_router,  # type: ignore[arg-type]
    )
