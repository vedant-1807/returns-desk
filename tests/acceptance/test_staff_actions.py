"""What an associate can do from the staff queue: clarify, or send a case to review."""

from __future__ import annotations

from typing import Any

from returns_desk.seeds import Ids
from tests.conftest import World

NORTHLINE_PAYMENT = Ids.payment_ref("northline", "1001")
QUESTION = "Can I return the blue jacket? It arrived damaged."


def send_to_review(
    world: World, user: str, merchant: str, case_id: str, note: str = "Third claim this month."
) -> Any:
    return world.call(
        "POST",
        f"/cases/{case_id}/review-request",
        user=user,
        merchant=merchant,
        json_body={"note": note},
    )


def test_an_associate_sends_an_otherwise_automatic_refund_to_a_supervisor(world: World):
    # $180 at Northline would be refunded automatically once the shopper confirms.
    case = world.say("maya", "northline", QUESTION)
    assert case["next_action"] == "confirm"

    response = send_to_review(world, "nina", "northline", case["id"])
    assert response.status_code == 200
    assert response.json()["already_requested"] is False
    assert response.json()["case"]["review_requested"]["by"] == str(Ids.user("nina"))

    # The shopper confirms. It now waits for a supervisor instead of paying out.
    case = world.say("maya", "northline", "Yes", case_id=case["id"])
    assert case["state"] == "awaiting_supervisor"
    assert world.processor.list_requests(NORTHLINE_PAYMENT) == []

    [review] = world.call("GET", "/reviews", user="sam", merchant="northline").json()["reviews"]
    assert review["reasons"] == ["staff_escalation"]
    assert review["amount_cents"] == 18000

    decided = world.call(
        "POST",
        f"/reviews/{review['review_id']}/decision",
        user="sam",
        merchant="northline",
        json_body={"decision": "approve", "note": "Fine."},
    )
    assert decided.status_code == 200
    assert [r.amount_cents for r in world.processor.list_refunds(NORTHLINE_PAYMENT)] == [18000]


def test_the_note_is_internal_and_the_shopper_is_told_nothing_about_why(world: World):
    case = world.say("maya", "northline", QUESTION)
    send_to_review(world, "nina", "northline", case["id"], note="Looks like wardrobing.")
    case = world.say("maya", "northline", "Yes", case_id=case["id"])

    mine = world.call("GET", f"/cases/{case['id']}", user="maya", merchant="northline").json()
    assert "wardrobing" not in mine["case"].__repr__()
    assert "review_requested" not in mine["case"]
    assert mine["case"]["status"] == "in_review"

    staff = world.call("GET", f"/cases/{case['id']}", user="nina", merchant="northline").json()
    notes = [m for m in staff["case"]["messages"] if m["audience"] == "internal"]
    assert [(n["author_kind"], n["body"]) for n in notes] == [
        ("associate", "Looks like wardrobing.")
    ]
    listed = world.call("GET", "/cases", user="nina", merchant="northline").json()["cases"]
    assert [c["review_requested"] for c in listed if c["id"] == case["id"]] == [True]


def test_sending_to_review_twice_changes_nothing(world: World):
    case = world.say("maya", "northline", QUESTION)

    first = send_to_review(world, "nina", "northline", case["id"])
    second = send_to_review(world, "nina", "northline", case["id"], note="Again.")

    assert first.json()["already_requested"] is False
    assert second.json()["already_requested"] is True
    events = world.rows(
        "select 1 from case_events where case_id = %s and kind = 'review_requested'", (case["id"],)
    )
    assert len(events) == 1


def test_only_this_merchants_staff_can_send_an_open_case_to_review(world: World):
    case = world.say("maya", "northline", QUESTION)
    path = f"/cases/{case['id']}/review-request"

    assert send_to_review(world, "maya", "northline", case["id"]).status_code == 403
    assert send_to_review(world, "alex", "vale", case["id"]).status_code == 404
    assert world.call("POST", path, json_body={"note": "x"}).status_code == 401
    assert (
        world.call(
            "POST", path, user="nina", merchant="northline", json_body={"note": ""}
        ).status_code
        == 422
    )

    # A finished case cannot be sent anywhere.
    done = world.say("maya", "northline", "Yes", case_id=case["id"])
    assert done["state"] == "resolved"
    assert send_to_review(world, "nina", "northline", case["id"]).status_code == 409
    assert world.rows("select 1 from cases where review_requested_at is not null") == []


def test_an_associate_can_write_to_the_shopper_to_clarify(world: World):
    case = world.say("maya", "northline", "the jacket came wrong")

    response = world.call(
        "POST",
        f"/cases/{case['id']}/messages",
        user="nina",
        merchant="northline",
        json_body={"client_request_id": "nina-1", "text": "Which jacket do you mean?"},
    )

    assert response.status_code == 200
    mine = world.call("GET", f"/cases/{case['id']}", user="maya", merchant="northline").json()
    assert mine["case"]["messages"][-1]["body"] == "Which jacket do you mean?"
    assert mine["case"]["messages"][-1]["author_kind"] == "associate"
