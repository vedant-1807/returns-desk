"""Stand-ins for the outside world that still exercise the real client code."""

from __future__ import annotations

import json
import time
from typing import Any
from urllib.parse import parse_qsl

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


class LoseResponses(httpx2.BaseTransport):
    """Wraps a transport and loses the answers to chosen requests.

    The request really goes out and the far end really acts on it. Only the response is
    dropped, which is exactly what a lost payment response is. It wraps the fake Stripe
    and the real one alike.
    """

    def __init__(self, inner: httpx2.BaseTransport) -> None:
        self._inner = inner
        # (method, path) pairs whose next response is to be lost.
        self.lose_next: list[tuple[str, str]] = []

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        response = self._inner.handle_request(request)
        key = (request.method, request.url.path)
        if key in self.lose_next:
            self.lose_next.remove(key)
            response.read()
            response.close()
            raise httpx2.ReadTimeout("the response was lost on the way back", request=request)
        return response

    def close(self) -> None:
        # The test owns the wrapped transport. An app restart must not close it.
        return None


class FakeStripe:
    """A stateful stand-in for the parts of Stripe's API the processor uses.

    It keeps payment intents and refunds, honours idempotency keys the way Stripe does
    (same key and same values replays the first answer; same key with other values is an
    error), refuses to refund more than was paid, and lists refunds newest first.
    """

    KEY = "sk_test_fake_key_for_tests"

    def __init__(self) -> None:
        self.payments: dict[str, dict[str, Any]] = {}
        self.refunds: list[dict[str, Any]] = []
        self._by_key: dict[str, tuple[dict[str, str], dict[str, Any]]] = {}
        self.requests: list[tuple[str, str]] = []
        # Status the next new refund is created with ("pending" until told otherwise).
        self.next_refund_status = "succeeded"
        # When set, every request is answered with this HTTP status and error.
        self.fail_with: tuple[int, dict[str, Any]] | None = None

    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self._handle)

    def finish(self, refund_id: str, status: str = "succeeded") -> None:
        """Move a pending refund to its final status, as Stripe does later on."""
        for refund in self.refunds:
            if refund["id"] == refund_id:
                refund["status"] = status

    def event(self, refund_id: str, event_type: str = "refund.updated") -> dict[str, Any]:
        """The webhook event Stripe would send about a refund in its current state."""
        [refund] = [r for r in self.refunds if r["id"] == refund_id]
        return {
            "id": f"evt_fake_{refund_id}_{refund['status']}",
            "object": "event",
            "type": event_type,
            "created": int(time.time()),
            "data": {"object": dict(refund)},
        }

    def _error(self, status: int, kind: str, code: str, message: str) -> httpx2.Response:
        return httpx2.Response(
            status, json={"error": {"type": kind, "code": code, "message": message}}
        )

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append((request.method, request.url.path))
        if request.headers.get("authorization") != f"Bearer {self.KEY}":
            return self._error(401, "invalid_request_error", "api_key_invalid", "Invalid API key")
        if self.fail_with is not None:
            status, error = self.fail_with
            return httpx2.Response(status, json={"error": error})
        form = dict(parse_qsl(request.content.decode()))
        route = (request.method, request.url.path)

        if route == ("POST", "/v1/payment_intents"):
            intent = {
                "id": f"pi_fake_{len(self.payments) + 1:04d}",
                "object": "payment_intent",
                "amount": int(form["amount"]),
                "currency": form["currency"],
                "status": "succeeded",
            }
            self.payments[intent["id"]] = intent
            return httpx2.Response(200, json=intent)

        if route == ("POST", "/v1/refunds"):
            key = request.headers.get("idempotency-key", "")
            if key in self._by_key:
                first_form, first_refund = self._by_key[key]
                if first_form != form:
                    return self._error(
                        400,
                        "idempotency_error",
                        "",
                        "Keys for idempotent requests can only "
                        "be used with the same parameters they were first used with.",
                    )
                # A replay returns the answer of the first request, as it was then.
                return httpx2.Response(
                    200, json=first_refund, headers={"Idempotent-Replayed": "true"}
                )
            payment = self.payments.get(form.get("payment_intent", ""))
            if payment is None:
                return self._error(
                    404, "invalid_request_error", "resource_missing", "No such payment_intent"
                )
            amount = int(form["amount"])
            refunded = sum(
                r["amount"]
                for r in self.refunds
                if r["payment_intent"] == payment["id"] and r["status"] != "failed"
            )
            if amount <= 0 or refunded + amount > payment["amount"]:
                return self._error(
                    400,
                    "invalid_request_error",
                    "charge_already_refunded",
                    "Refund amount is greater than the unrefunded amount on the charge.",
                )
            refund = {
                "id": f"re_fake_{len(self.refunds) + 1:04d}",
                "object": "refund",
                "amount": amount,
                "currency": payment["currency"],
                "payment_intent": payment["id"],
                "status": self.next_refund_status,
                "created": int(time.time()),
                "metadata": {
                    name[len("metadata[") : -1]: value
                    for name, value in form.items()
                    if name.startswith("metadata[")
                },
            }
            self.refunds.append(refund)
            self._by_key[key] = (form, dict(refund))
            return httpx2.Response(200, json=refund)

        if route == ("GET", "/v1/refunds"):
            wanted = request.url.params.get("payment_intent")
            found = [r for r in reversed(self.refunds) if r["payment_intent"] == wanted]
            return httpx2.Response(200, json={"object": "list", "data": found, "has_more": False})

        return self._error(404, "invalid_request_error", "url_invalid", "Unrecognized URL")
