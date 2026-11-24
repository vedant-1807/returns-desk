"""The HTTP model gateway against a scripted upstream: no network, no real waiting."""

from __future__ import annotations

import json
from typing import Any

import httpx2

from returns_desk.models.gateway import ModelRequest, Route, RoutedGateway

REQUEST = ModelRequest(
    purpose="interpret",
    system="Fill in the form.",
    user="The blue jacket arrived damaged.",
    schema_name="form",
    schema={"type": "object", "properties": {"intent": {"type": "string"}}},
)

Step = tuple[int, dict[str, str], dict[str, Any]]


def answer(data: dict[str, Any]) -> Step:
    return (
        200,
        {},
        {
            "model": "served-model",
            "choices": [{"message": {"content": json.dumps(data)}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        },
    )


def rate_limited(retry_after: float | None = None) -> Step:
    headers = {} if retry_after is None else {"retry-after": str(retry_after)}
    return (429, headers, {"error": {"message": "slow down"}})


class Upstream:
    """Plays a list of responses in order (the last one repeats) and records what the
    gateway sent and how long it chose to wait."""

    def __init__(self, *steps: Step) -> None:
        self._steps = list(steps)
        self.requests: list[httpx2.Request] = []
        self.sleeps: list[float] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        status, headers, body = self._steps.pop(0) if len(self._steps) > 1 else self._steps[0]
        return httpx2.Response(status, headers=headers, json=body)

    def gateway(self, *routes: str, attempts: int = 2, wait: float = 0.0) -> RoutedGateway:
        return RoutedGateway(
            [
                Route(
                    name,
                    f"https://{name}.test/v1",
                    f"key-{name}",
                    "low-model",
                    "strong-model",
                    extra={"reasoning_effort": "low"},
                )
                for name in routes or ("first",)
            ],
            attempts_per_turn=attempts,
            deadline_seconds=30,
            rate_limit_wait_seconds=wait,
            sleep=self.sleeps.append,
            transport=httpx2.MockTransport(self),
        )

    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]


def run(gateway: RoutedGateway, validate=lambda data: None):
    return gateway.complete(REQUEST, gateway.new_budget(), validate)


def outcomes(result) -> list[tuple[str, str]]:
    return [(attempt.route, attempt.outcome) for attempt in result.attempts]


def test_a_valid_answer_comes_back_with_the_model_and_tokens_the_upstream_reported():
    upstream = Upstream(answer({"intent": "refund"}))
    result = run(upstream.gateway())

    assert result.data == {"intent": "refund"}
    [attempt] = result.attempts
    assert (attempt.outcome, attempt.model) == ("ok", "served-model")
    assert (attempt.input_tokens, attempt.output_tokens) == (120, 30)


def test_the_request_asks_for_strict_json_and_carries_the_route_settings():
    upstream = Upstream(answer({"intent": "refund"}))
    run(upstream.gateway())

    [request] = upstream.requests
    body = json.loads(request.content)
    assert request.url.path == "/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer key-first"
    assert body["model"] == "low-model"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"] == REQUEST.schema
    assert body["reasoning_effort"] == "low"
    # The model is given text and a form to fill in, and nothing it could call.
    assert "tools" not in body


def test_a_live_chat_fails_over_at_once_when_a_route_is_rate_limited():
    upstream = Upstream(rate_limited(3), answer({"intent": "refund"}))
    result = run(upstream.gateway("first", "second"))

    assert result.ok
    assert outcomes(result) == [("first", "rate_limited"), ("second", "ok")]
    assert upstream.hosts() == ["first.test", "second.test"]
    assert upstream.sleeps == []


def test_a_batch_job_waits_out_a_rate_limit_and_asks_the_same_route_again():
    upstream = Upstream(rate_limited(3), answer({"intent": "refund"}))
    # One attempt per turn: the answer only arrives if the refused request cost nothing.
    gateway = upstream.gateway("first", "second", attempts=1, wait=60)
    result = run(gateway)

    assert result.ok
    assert outcomes(result) == [("first", "rate_limited"), ("first", "ok")]
    assert upstream.hosts() == ["first.test", "first.test"]
    assert upstream.sleeps == [3.25]
    assert "waited 3.2" in result.attempts[0].error
    assert (gateway.rate_limit_waits, gateway.waited_seconds) == (1, 3.25)


def test_a_wait_longer_than_the_cap_is_refused_and_the_route_fails_over():
    upstream = Upstream(rate_limited(600), answer({"intent": "refund"}))
    result = run(upstream.gateway("first", "second", wait=60))

    assert outcomes(result) == [("first", "rate_limited"), ("second", "ok")]
    assert upstream.sleeps == []


def test_a_rate_limit_that_names_no_wait_fails_over():
    upstream = Upstream(rate_limited(), answer({"intent": "refund"}))
    result = run(upstream.gateway("first", "second", wait=60))

    assert outcomes(result) == [("first", "rate_limited"), ("second", "ok")]
    assert upstream.sleeps == []


def test_waiting_is_bounded_when_the_upstream_never_lets_the_request_in():
    upstream = Upstream(rate_limited(1))
    result = run(upstream.gateway(wait=60))

    assert not result.ok
    # Four waits, then the two attempts of the turn are spent as usual.
    assert upstream.sleeps == [1.25] * 4
    assert [outcome for _, outcome in outcomes(result)] == ["rate_limited"] * 6 + [
        "budget_exhausted"
    ]


def test_an_answer_the_validator_rejects_is_a_failed_attempt_on_the_same_route():
    upstream = Upstream(answer({"intent": "fly"}), answer({"intent": "refund"}))

    def validate(data: dict[str, Any]) -> None:
        if data["intent"] != "refund":
            raise ValueError("not an intent we know")

    result = run(upstream.gateway("first", "second"), validate)

    assert result.data == {"intent": "refund"}
    assert outcomes(result) == [("first", "invalid_output"), ("first", "ok")]


def test_a_server_error_fails_over_and_running_out_of_attempts_returns_nothing():
    upstream = Upstream((503, {}, {"error": "down"}))
    result = run(upstream.gateway("first", "second"))

    assert result.data is None
    assert outcomes(result) == [
        ("first", "error"),
        ("second", "error"),
        ("first", "budget_exhausted"),
    ]


def test_a_blocked_budget_reaches_no_upstream_and_says_why():
    upstream = Upstream(answer({"intent": "refund"}))
    gateway = upstream.gateway()
    budget = gateway.new_budget()
    budget.block("daily_token_cap")

    result = gateway.complete(REQUEST, budget, lambda data: None)

    assert not result.ok
    assert upstream.requests == []
    [attempt] = result.attempts
    assert (attempt.outcome, attempt.error) == ("budget_exhausted", "daily_token_cap")


def routed(upstream: Upstream, router: str) -> RoutedGateway:
    return RoutedGateway(
        [Route("first", "https://first.test/v1", "key", "low-model", "strong-model")],
        attempts_per_turn=2,
        deadline_seconds=30,
        router=router,  # type: ignore[arg-type]
        transport=httpx2.MockTransport(upstream),
    )


def test_the_router_setting_decides_which_tier_a_call_uses():
    models = {}
    for router in ("routed", "always_strong", "always_low"):
        upstream = Upstream(answer({"intent": "refund"}))
        gateway = routed(upstream, router)
        result = run(gateway)
        models[router] = (
            json.loads(upstream.requests[0].content)["model"],
            result.attempts[0].tier,
            gateway.has_distinct_strong_tier(),
        )

    assert models == {
        # The caller asked for the low tier; only "routed" leaves escalation open.
        "routed": ("low-model", "low", True),
        "always_strong": ("strong-model", "strong", False),
        "always_low": ("low-model", "low", False),
    }
