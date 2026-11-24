"""Recording a conversation and running it again.

A recorded run is replayed through the real service in its own database, with the
recorded model answers in place of a model. The same outcome must come back; a changed
model answer must show up as a difference.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from uuid import UUID

from returns_desk.tracing import bundle as bundles
from tests.conftest import REFERENCE_NOW, World

REFERENCE = "I ordered two jackets. The blue one arrived damaged. Please refund just that one."


def record(world: World, case_id: str) -> dict:
    return bundles.record(
        world.settings.admin_dsn,
        UUID(case_id),
        fixture="two_jackets",
        seeded_at=REFERENCE_NOW,
        embedder="hash",
    )


def decide(world: World, case_id: str, decision: str) -> None:
    reviews = world.call("GET", "/reviews", user="vera", merchant="vale").json()["reviews"]
    [review] = [r for r in reviews if r["case_id"] == case_id]
    response = world.call(
        "POST",
        f"/reviews/{review['review_id']}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": decision, "note": "Checked the photo."},
    )
    assert response.status_code == 200


def test_a_refund_with_a_supervisor_review_replays_to_the_same_outcome(world: World):
    case = world.say("maya", "vale", REFERENCE)
    world.clock.advance(timedelta(hours=3))
    decide(world, case["id"], "approve")

    bundle = record(world, case["id"])

    assert [step["kind"] for step in bundle["steps"]] == ["message", "decision"]
    assert bundle["expected"]["state"] == "resolved"
    assert bundle["expected"]["refunded_cents"] == 18000
    assert [o["purpose"] for o in bundle["model_outputs"]] == ["interpret"]

    result = bundles.replay(world.settings, bundle)

    assert result["differences"] == []
    assert result["matches"] is True
    assert result["replayed"] == bundle["expected"]
    # It really ran again, as a new case in the replay database.
    assert result["replayed_case_id"] != case["id"]


def test_a_conversation_with_a_question_and_a_confirmation_replays(world: World):
    case = world.say("maya", "northline", "Can I return the blue jacket? It arrived damaged.")
    world.clock.advance(timedelta(minutes=5))
    case = world.say("maya", "northline", "Yes", case_id=case["id"])
    assert case["state"] == "resolved"

    bundle = record(world, case["id"])
    result = bundles.replay(world.settings, bundle)

    assert result["differences"] == []
    assert [turn["outcome"] for turn in result["replayed"]["turns"]] == ["asked", "processing"]


def test_a_policy_answer_replays_with_its_recorded_words(world: World):
    case = world.say("maya", "northline", "How long do I have to return things?")

    bundle = record(world, case["id"])
    assert [o["purpose"] for o in bundle["model_outputs"]] == ["interpret", "answer"]
    result = bundles.replay(world.settings, bundle)

    assert result["differences"] == []
    assert result["replayed"]["outcome_reason"] == "policy_question"


def test_a_different_model_answer_shows_up_as_a_difference(world: World):
    case = world.say("maya", "northline", REFERENCE)
    bundle = record(world, case["id"])

    # The same conversation, but this time the model does not see a clear request.
    tampered = copy.deepcopy(bundle)
    tampered["model_outputs"][0]["data"]["explicit_request"] = False
    result = bundles.replay(world.settings, tampered)

    assert result["matches"] is False
    assert any(d.startswith("state:") for d in result["differences"])
    assert result["replayed"]["refunded_cents"] == 0
