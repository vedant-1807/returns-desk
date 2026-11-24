"""The operations view: one merchant's numbers, and only that merchant's."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from psycopg.types.json import Jsonb

from returns_desk.seeds import Ids
from tests.conftest import World
from tests.fakes import ModelUpstreams

REFERENCE = "I ordered two jackets. The blue one arrived damaged. Please refund just that one."


def summary(world: World, user: str, merchant: str) -> dict[str, Any]:
    response = world.call("GET", "/ops/summary", user=user, merchant=merchant)
    assert response.status_code == 200, response.text
    return response.json()["summary"]


def test_each_merchant_sees_its_own_backlog_payments_and_usage(world: World):
    # Northline: one automatic refund, and one refund whose payment result is unknown.
    world.say("maya", "northline", REFERENCE)
    world.processor.arm_fault("lost_response", payment_ref=Ids.payment_ref("northline", "2001"))
    stuck = world.say("omar", "northline", "The tote arrived damaged. Please refund it.")
    assert stuck["state"] == "reconciling"
    # Vale: the same request waits for a supervisor.
    world.say("maya", "vale", REFERENCE)
    world.clock.advance(timedelta(minutes=30))

    northline = summary(world, "sam", "northline")
    assert northline["merchant"] == "northline"
    assert northline["cases"]["by_state"] == {"resolved": 1, "reconciling": 1}
    assert northline["review_backlog"] == {"waiting": 0, "oldest_waiting_seconds": None}
    assert northline["uncertain_payments"] == {"reconciling": 1, "oldest_seconds": 1800.0}
    assert northline["automatic_resolutions"]["count"] == 1
    assert northline["latency"]["turns"] == 2
    assert northline["model_usage"]["attempts"] == 2
    assert northline["model_usage"]["cost_complete"] is True

    vale = summary(world, "alex", "vale")
    assert vale["cases"]["by_state"] == {"awaiting_supervisor": 1}
    assert vale["review_backlog"] == {"waiting": 1, "oldest_waiting_seconds": 1800.0}
    assert vale["uncertain_payments"]["reconciling"] == 0
    assert vale["automatic_resolutions"]["count"] == 0
    assert vale["model_usage"]["attempts"] == 1

    assert world.call("GET", "/ops/summary", user="maya", merchant="vale").status_code == 403
    assert world.call("GET", "/ops/summary").status_code == 401


def test_usage_with_no_known_price_is_shown_not_guessed(world: World):
    # A route that is not on the price list.
    world.model = ModelUpstreams(primary="ok").gateway()  # type: ignore[assignment]
    world.restart()
    world.say("maya", "northline", REFERENCE)

    usage = summary(world, "sam", "northline")["model_usage"]

    assert usage["input_tokens"] == 100 and usage["output_tokens"] == 20
    assert usage["unpriced_attempts"] == 1
    assert usage["cost_complete"] is False
    assert usage["list_price_usd"] == 0
    assert usage["by_model"]["primary/primary-low"]["priced"] is False


def test_a_case_flagged_for_staff_is_counted_until_someone_finishes_it(world: World):
    world.script["interpret"] = TimeoutError("model is down")
    world.say("maya", "northline", REFERENCE)

    assert summary(world, "nina", "northline")["needs_staff"] == 1
    assert summary(world, "alex", "vale")["needs_staff"] == 0


def test_the_latest_eval_run_is_reported_per_merchant(world: World):
    assert summary(world, "sam", "northline")["evaluated_cases"] is None

    with world.admin() as conn:
        conn.execute(
            """
            insert into eval_runs (suite, label, seed, cases, metrics, by_merchant)
            values ('smoke', 'test run', 7, 30, %s, %s)
            """,
            (
                Jsonb(
                    {
                        "final_state_accuracy": 0.9667,
                        "cost": {"per_correct_automatic_resolution_usd": 0.00024, "complete": True},
                    }
                ),
                Jsonb(
                    {
                        "northline": {"cases": 16, "correct": 16, "incorrect_financial_actions": 0},
                        "vale": {"cases": 14, "correct": 13, "incorrect_financial_actions": 0},
                    }
                ),
            ),
        )
    try:
        evaluated = summary(world, "vera", "vale")["evaluated_cases"]
        assert (evaluated["cases"], evaluated["correct"]) == (14, 13)
        assert evaluated["incorrect_financial_actions"] == 0
        assert evaluated["cost_per_correct_automatic_resolution_usd"] == 0.00024
        assert evaluated["label"] == "test run"
    finally:
        with world.admin() as conn:
            conn.execute("delete from eval_runs")
