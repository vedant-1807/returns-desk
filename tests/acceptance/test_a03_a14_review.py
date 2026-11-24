"""A03 and A14: supervisor review.

The same request that Northline refunds automatically waits for a human at Vale, because
$180 is above Vale's $150 review line. Nothing reaches the payment processor while it
waits, and only a supervisor of that merchant can decide it.
"""

from __future__ import annotations

from typing import Any

import pytest

from returns_desk.seeds import Ids
from tests.conftest import World

pytestmark = pytest.mark.acceptance

VALE_PAYMENT = Ids.payment_ref("vale", "1001")


def open_vale_case(world: World) -> dict[str, Any]:
    response = world.call(
        "POST", "/cases", user="maya", merchant="vale", json_body=world.refund_request("vale")
    )
    assert response.status_code == 200
    return response.json()["case"]


def pending_review(world: World) -> dict[str, Any]:
    reviews = world.call("GET", "/reviews", user="vera", merchant="vale").json()["reviews"]
    assert len(reviews) == 1
    return reviews[0]


def assert_no_financial_effect(world: World) -> None:
    assert world.processor.list_requests(VALE_PAYMENT) == []
    assert world.processor.list_refunds(VALE_PAYMENT) == []
    assert world.actions("vale") == []
    assert world.ledger("vale") == []
    blue = world.line("vale", "1001", "blue")
    assert (blue["reserved_quantity"], blue["reserved_cents"]) == (0, 0)
    assert (blue["refunded_quantity"], blue["refunded_cents"]) == (0, 0)


def test_a03_same_request_at_vale_waits_for_review(world: World):
    case = open_vale_case(world)

    assert case["state"] == "awaiting_supervisor"
    assert case["status"] == "in_review"
    assert case["amount_cents"] == 18000
    assert case["receipt"] is None
    # The shopper sees neutral wording and no internal detail.
    assert "review" not in case
    assert "proposal" not in case
    assert "limit" not in str(case).lower()

    # No provider call while pending; the payment ledger stays empty.
    assert_no_financial_effect(world)

    # The supervisor sees the exact proposal with the internal rule and the reason.
    review = pending_review(world)
    assert review["case_id"] == case["id"]
    assert review["amount_cents"] == 18000
    assert review["proposed_action"] == "refund"
    assert review["item"]["variant"] == "Blue / M"
    assert review["reasons"] == ["above_auto_limit"]
    assert review["internal_evidence"]["auto_approve_limit_cents"] == 15000
    assert review["internal_rule"]["rules"]["auto_approve_limit_cents"] == 15000
    assert [s["section_key"] for s in review["applicable_policy"]] == [
        "returns.window",
        "returns.damaged",
    ]

    # Reconcile has nothing to run: review is not a queued payment.
    assert world.reconcile().json()["result"] == {"claimed": 0}
    assert_no_financial_effect(world)


def test_a03_approval_produces_exactly_one_refund(world: World):
    case = open_vale_case(world)
    review = pending_review(world)

    response = world.call(
        "POST",
        f"/reviews/{review['review_id']}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "approve", "note": "Photo shows a torn seam."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decision"]["decision"] == "approved"
    assert body["decision"]["already_decided"] is False
    assert body["case"]["state"] == "resolved"

    refunds = world.processor.list_refunds(VALE_PAYMENT)
    assert [r.amount_cents for r in refunds] == [18000]
    actions = world.actions("vale")
    assert [(a["status"], a["amount_cents"]) for a in actions] == [("succeeded", 18000)]
    assert [entry["amount_cents"] for entry in world.ledger("vale")] == [18000]
    assert actions[0]["provider_refund_id"] == refunds[0].id

    # Who decided, which version, when and why are all on record.
    approvals = world.rows("select * from approvals")
    assert len(approvals) == 1
    assert approvals[0]["reviewer_user_id"] == Ids.user("vera")
    assert approvals[0]["decision"] == "approved"
    assert approvals[0]["proposal_version"] == review["proposal_version"] == 1
    assert approvals[0]["note"] == "Photo shows a torn seam."
    assert approvals[0]["request_id"] == response.headers["x-request-id"]

    # A second click, by the same or another supervisor, changes nothing.
    again = world.call(
        "POST",
        f"/reviews/{review['review_id']}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "decline", "note": "changed my mind"},
    )
    assert again.status_code == 200
    assert again.json()["decision"]["decision"] == "approved"
    assert again.json()["decision"]["already_decided"] is True
    assert len(world.processor.list_refunds(VALE_PAYMENT)) == 1
    assert len(world.rows("select 1 from approvals")) == 1

    # The shopper sees the refund, and nothing is left in the queue.
    shopper_view = world.call("GET", f"/cases/{case['id']}", user="maya", merchant="vale").json()
    assert shopper_view["case"]["status"] == "refunded"
    assert shopper_view["case"]["receipt"]["refund_reference"] == refunds[0].id
    assert world.call("GET", "/reviews", user="vera", merchant="vale").json()["reviews"] == []

    # The green jacket is untouched at Vale too.
    green = world.line("vale", "1001", "green")
    assert (green["reserved_cents"], green["refunded_cents"]) == (0, 0)


def test_a03_decline_produces_no_refund(world: World):
    case = open_vale_case(world)
    review = pending_review(world)

    response = world.call(
        "POST",
        f"/reviews/{review['review_id']}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "decline", "note": "No damage visible in the photo."},
    )

    assert response.status_code == 200
    assert response.json()["decision"]["decision"] == "declined"
    assert response.json()["case"]["state"] == "declined"

    # Zero refunds, zero provider calls, balance preserved.
    assert_no_financial_effect(world)
    world.reconcile()
    assert_no_financial_effect(world)

    # The shopper is told it was not approved, without the supervisor's internal note.
    shopper_view = world.call("GET", f"/cases/{case['id']}", user="maya", merchant="vale").json()
    assert shopper_view["case"]["status"] == "declined"
    assert shopper_view["case"]["outcome"] == {"type": "declined_by_supervisor", "reason": None}
    assert "No damage visible" not in str(shopper_view)

    # An approval after the decline cannot reopen it.
    late = world.call(
        "POST",
        f"/reviews/{review['review_id']}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "approve"},
    )
    assert late.json()["decision"]["decision"] == "declined"
    assert late.json()["decision"]["already_decided"] is True
    assert_no_financial_effect(world)


def test_a03_approval_survives_a_restart_while_pending(world: World):
    open_vale_case(world)
    review_id = pending_review(world)["review_id"]

    # The pending proposal lives in Postgres, not in process memory.
    world.restart()
    assert pending_review(world)["review_id"] == review_id

    response = world.call(
        "POST",
        f"/reviews/{review_id}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "approve"},
    )
    assert response.json()["case"]["state"] == "resolved"
    assert len(world.processor.list_refunds(VALE_PAYMENT)) == 1


def test_a03_a_hard_constraint_cannot_be_approved_away(world: World):
    open_vale_case(world)
    review = pending_review(world)

    # While the case waits, the unit gets refunded some other way.
    world.rows(
        "update order_lines set refunded_quantity = 1, refunded_cents = 18000 "
        "where id = %s returning id",
        (Ids.line("vale", "1001", "blue"),),
    )

    response = world.call(
        "POST",
        f"/reviews/{review['review_id']}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "approve"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"]["hard_constraint"] == "quantity_unavailable"
    assert world.processor.list_requests(VALE_PAYMENT) == []
    assert world.actions("vale") == []
    assert world.rows("select 1 from approvals") == []
    assert world.rows("select state from cases")[0]["state"] == "declined"


def test_a14_only_a_supervisor_of_the_same_merchant_can_decide(world: World):
    case = open_vale_case(world)
    review_id = pending_review(world)["review_id"]
    path = f"/reviews/{review_id}/decision"
    approve = {"decision": "approve"}

    # Not signed in at all.
    assert world.call("POST", path, json_body=approve).status_code == 401
    # A token signed with the wrong key.
    forged = world.call(
        "POST", path, headers={"Authorization": "Bearer not.a.real-token"}, json_body=approve
    )
    assert forged.status_code == 401

    # A supervisor from another merchant: the review does not exist as far as they can tell.
    other = world.call("POST", path, user="sam", merchant="northline", json_body=approve)
    unknown = world.call(
        "POST",
        "/reviews/00000000-0000-4000-8000-000000000000/decision",
        user="sam",
        merchant="northline",
        json_body=approve,
    )
    assert other.status_code == unknown.status_code == 404
    assert other.json() == unknown.json()

    # The same supervisor with a token that claims Vale: the claim is checked against the
    # database, where they have no Vale membership.
    assert (
        world.call("POST", path, user="sam", merchant="vale", json_body=approve).status_code == 403
    )

    # An associate of the right merchant.
    assert (
        world.call("POST", path, user="alex", merchant="vale", json_body=approve).status_code == 403
    )
    assert world.call("GET", "/reviews", user="alex", merchant="vale").status_code == 403

    # The shopper who owns the case.
    assert (
        world.call("POST", path, user="maya", merchant="vale", json_body=approve).status_code == 403
    )
    assert world.call("GET", "/reviews", user="maya", merchant="vale").status_code == 403

    # None of that decided anything or touched money.
    assert world.rows("select 1 from approvals") == []
    assert world.rows("select state from cases where id = %s", (case["id"],))[0]["state"] == (
        "awaiting_supervisor"
    )
    assert_no_financial_effect(world)

    # A valid supervisor can still review.
    valid = world.call("POST", path, user="vera", merchant="vale", json_body=approve)
    assert valid.status_code == 200
    assert valid.json()["case"]["state"] == "resolved"
    assert len(world.processor.list_refunds(VALE_PAYMENT)) == 1
    assert world.rows("select reviewer_user_id from approvals")[0]["reviewer_user_id"] == (
        Ids.user("vera")
    )
