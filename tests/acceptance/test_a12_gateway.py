"""A12: the model gateway under failure.

Given the primary model timing out, both routes failing, or the daily budget being used
up, the service gives a bounded fallback or a hand-off, records every attempt, and makes
no change it has not verified.

These tests run the production gateway code over HTTP. Only the far end of each call is
scripted (see tests/fakes.py).
"""

from __future__ import annotations

from typing import Any

import pytest

from returns_desk.seeds import Ids
from tests.conftest import World
from tests.fakes import ModelUpstreams

pytestmark = pytest.mark.acceptance

REFERENCE = "I ordered two jackets. The blue one arrived damaged. Please refund just that one."
NORTHLINE_PAYMENT = Ids.payment_ref("northline", "1001")


def use(world: World, upstreams: ModelUpstreams, **options: Any) -> None:
    """Swap the scripted stand-in for the real gateway and restart the app around it."""
    world.model = upstreams.gateway(**options)  # type: ignore[assignment]
    world.restart()


def attempts(world: World, case_id: str | None = None) -> list[tuple[str, str]]:
    rows = world.rows(
        "select route, outcome from model_attempts "
        "where %(case)s::uuid is null or case_id = %(case)s::uuid order by id",
        {"case": case_id},
    )
    return [(r["route"], r["outcome"]) for r in rows]


def last_reply(case: dict[str, Any]) -> str:
    replies = [m["body"] for m in case["messages"] if m["author_kind"] != "shopper"]
    return replies[-1] if replies else ""


def assert_nothing_moved(world: World) -> None:
    assert world.processor.list_requests(NORTHLINE_PAYMENT) == []
    assert world.actions("northline") == []
    assert world.ledger("northline") == []
    assert world.rows("select 1 from proposals") == []
    assert (
        world.rows(
            "select 1 from order_lines where reserved_quantity <> 0 or refunded_quantity <> 0"
        )
        == []
    )


def test_a12_the_primary_model_times_out_and_the_second_route_answers(world: World):
    upstreams = ModelUpstreams(primary="timeout", backup="ok")
    use(world, upstreams)

    response = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body={"client_request_id": "a12-failover", "message": REFERENCE},
    )
    case = response.json()["case"]

    # Bounded: one try on each route, inside the two-attempt budget of a turn.
    assert upstreams.calls == [("primary", "timeout"), ("backup", "ok")]
    assert case["state"] == "resolved"
    assert [r.amount_cents for r in world.processor.list_refunds(NORTHLINE_PAYMENT)] == [18000]

    # Every attempt is on record, the failed one included, under the request's own ID.
    rows = world.rows(
        "select route, model, outcome, latency_ms, request_id from model_attempts order by id"
    )
    assert [(r["route"], r["outcome"]) for r in rows] == [("primary", "timeout"), ("backup", "ok")]
    assert {r["request_id"] for r in rows} == {response.headers["x-request-id"]}
    assert all(r["latency_ms"] is not None for r in rows)

    # And in the trace a staff member reads, failed attempt first, flagged as a warning.
    trace = world.call(
        "GET", f"/cases/{case['id']}/trace", user="sam", merchant="northline"
    ).json()["trace"]
    generations = [s for s in trace["spans"] if s["kind"] == "generation"]
    assert [
        (g["attributes"]["route"], g["attributes"]["outcome"], g["level"]) for g in generations
    ] == [
        ("primary", "timeout", "WARNING"),
        ("backup", "ok", "DEFAULT"),
    ]
    assert trace["totals"]["model_attempts_not_ok"] == 1


def test_a12_both_routes_fail_and_a_person_takes_over(world: World):
    upstreams = ModelUpstreams(primary="timeout", backup="error")
    use(world, upstreams)

    case = world.say("maya", "northline", REFERENCE)

    assert upstreams.calls == [("primary", "timeout"), ("backup", "error")]
    assert attempts(world) == [
        ("primary", "timeout"),
        ("backup", "error"),
        ("primary", "budget_exhausted"),
    ]
    # No guess and no payment: the shopper is told, and the case is flagged for staff.
    assert case["state"] == "open"
    assert "passed your message to our team" in last_reply(case)
    flagged = world.rows(
        "select detail from case_events where case_id = %s and kind = 'needs_staff'",
        (case["id"],),
    )
    assert [f["detail"]["reason"] for f in flagged] == ["agent_unavailable"]
    assert_nothing_moved(world)


def test_a12_a_rate_limited_route_fails_over_without_waiting(world: World):
    upstreams = ModelUpstreams(primary="rate_limited", backup="ok")
    use(world, upstreams)

    case = world.say("maya", "northline", REFERENCE)

    assert attempts(world) == [("primary", "rate_limited"), ("backup", "ok")]
    assert case["state"] == "resolved"


def test_a12_at_the_daily_budget_no_model_is_called(world: World):
    upstreams = ModelUpstreams(primary="ok")
    use(world, upstreams)
    # Northline may spend 100 tokens a day. Vale has no cap of its own.
    with world.admin() as conn:
        conn.execute(
            "update merchants set daily_token_cap = 100 where id = %s", (Ids.merchant("northline"),)
        )

    # The first turn is inside the budget and uses 120 tokens.
    first = world.say("maya", "northline", "How long do I have to return things?")
    assert first["status"] == "answered"
    calls_so_far = len(upstreams.calls)

    # Now the budget is spent. The reference request reaches no model.
    case = world.say("maya", "northline", REFERENCE)

    assert len(upstreams.calls) == calls_so_far
    blocked = world.rows(
        "select outcome, error from model_attempts where case_id = %s", (case["id"],)
    )
    assert [(b["outcome"], b["error"]) for b in blocked] == [
        ("budget_exhausted", "daily_token_cap")
    ]
    assert case["state"] == "open"
    assert "passed your message to our team" in last_reply(case)
    assert world.actions("northline") == []
    assert world.processor.list_requests(NORTHLINE_PAYMENT) == []

    # The cap is per merchant: the same shopper at Vale is still served.
    at_vale = world.say("maya", "vale", REFERENCE)
    assert at_vale["state"] == "awaiting_supervisor"
    assert len(upstreams.calls) == calls_so_far + 1


def test_a12_a_yes_is_still_read_by_rule_when_the_budget_is_gone(world: World):
    upstreams = ModelUpstreams(primary="ok")
    use(world, upstreams)

    case = world.say("maya", "northline", "Can I return the blue jacket? It arrived damaged.")
    assert case["next_action"] == "confirm"
    calls_so_far = len(upstreams.calls)

    with world.admin() as conn:
        conn.execute("update merchants set daily_token_cap = 1")

    # Consent to an amount already quoted needs no model. Code rechecks everything.
    case = world.say("maya", "northline", "Yes", case_id=case["id"])

    assert len(upstreams.calls) == calls_so_far
    assert case["state"] == "resolved"
    assert [r.amount_cents for r in world.processor.list_refunds(NORTHLINE_PAYMENT)] == [18000]


def test_a12_a_policy_question_is_answered_by_quoting_when_the_answer_call_fails(world: World):
    # The interpret call succeeds; the call that would write the answer does not.
    upstreams = ModelUpstreams(primary=["ok", "timeout"], backup="error")
    use(world, upstreams, attempts_per_turn=3)

    case = world.say("maya", "northline", "How long do I have to return things?")

    assert case["status"] == "answered"
    # Grounded by construction: the reply is the policy passage itself, with its source.
    assert "within" in last_reply(case) and "Source:" in last_reply(case)
    assert attempts(world, case["id"]) == [
        ("primary", "ok"),
        ("primary", "timeout"),
        ("backup", "error"),
        ("primary", "budget_exhausted"),
    ]
    assert_nothing_moved(world)
