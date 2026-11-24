"""A01 and A02: the first build ticket.

A confirmed refund for the blue jacket produces exactly one $180 refund, and still exactly
one after the processor's response is lost and everything is retried. The green jacket is
never touched. Each assertion is made on persisted state, and the processor's own records
are checked separately from the application's.
"""

from __future__ import annotations

import pytest

from returns_desk.seeds import Ids
from tests.conftest import World

pytestmark = pytest.mark.acceptance

NORTHLINE_PAYMENT = Ids.payment_ref("northline", "1001")


def assert_exactly_one_180_refund(world: World) -> str:
    """Both systems, checked independently. Returns the processor's refund ID."""
    # The processor's side.
    refunds = world.processor.list_refunds(NORTHLINE_PAYMENT)
    assert len(refunds) == 1
    assert refunds[0].amount_cents == 18000
    assert refunds[0].status == "succeeded"

    # The application's side.
    actions = world.actions("northline")
    assert [(a["status"], a["amount_cents"]) for a in actions] == [("succeeded", 18000)]
    assert actions[0]["order_line_id"] == Ids.line("northline", "1001", "blue")
    ledger = world.ledger("northline")
    assert [(entry["amount_cents"], entry["order_line_id"]) for entry in ledger] == [
        (18000, Ids.line("northline", "1001", "blue"))
    ]

    # And they agree with each other.
    assert actions[0]["provider_refund_id"] == refunds[0].id == ledger[0]["provider_refund_id"]
    assert refunds[0].metadata["action_id"] == str(actions[0]["id"])

    blue = world.line("northline", "1001", "blue")
    assert (blue["refunded_quantity"], blue["refunded_cents"]) == (1, 18000)
    assert (blue["reserved_quantity"], blue["reserved_cents"]) == (0, 0)
    return refunds[0].id


def assert_green_line_untouched(world: World) -> None:
    green = world.line("northline", "1001", "green")
    assert (green["reserved_quantity"], green["reserved_cents"]) == (0, 0)
    assert (green["refunded_quantity"], green["refunded_cents"]) == (0, 0)
    assert green["quantity"] * green["unit_paid_cents"] == 12000


def test_a01_partial_refund_of_the_blue_jacket(world: World):
    response = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    )

    assert response.status_code == 200
    case = response.json()["case"]
    assert case["state"] == "resolved"
    assert case["status"] == "refunded"
    assert case["amount_cents"] == 18000
    assert case["item"]["variant"] == "Blue / M"
    assert case["item"]["quantity"] == 1

    refund_id = assert_exactly_one_180_refund(world)
    assert_green_line_untouched(world)

    # The shopper's receipt points at the processor's record.
    assert case["receipt"]["refund_reference"] == refund_id
    assert case["receipt"]["amount_cents"] == 18000

    # The decision cites customer-visible policy text.
    assert [c["section_key"] for c in case["policy_citations"]] == [
        "returns.window",
        "returns.damaged",
    ]

    # What the shopper can still return afterwards.
    orders = world.call("GET", "/orders", user="maya", merchant="northline").json()["orders"]
    lines = {line["variant"]: line for order in orders for line in order["lines"]}
    assert lines["Blue / M"]["remaining_cents"] == 0
    assert lines["Green / M"]["remaining_cents"] == 12000

    # The state change is on the audit trail with the HTTP request that caused it.
    events = world.rows(
        "select kind, request_id from case_events where case_id = %s order by id", (case["id"],)
    )
    assert [e["kind"] for e in events] == ["case_opened", "refund_authorized", "refund_confirmed"]
    assert events[1]["request_id"] == response.headers["x-request-id"]


def test_a01_asking_again_returns_the_original_receipt(world: World):
    body = world.refund_request("northline")
    first = world.call("POST", "/cases", user="maya", merchant="northline", json_body=body).json()

    # Same client request (a refresh): the same case, nothing new.
    refresh = world.call("POST", "/cases", user="maya", merchant="northline", json_body=body).json()
    assert refresh["replayed"] is True
    assert refresh["case"]["id"] == first["case"]["id"]
    assert refresh["case"]["receipt"] == first["case"]["receipt"]

    # A brand new request about the same jacket: the existing resolution is surfaced.
    again = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    ).json()
    assert again["existing_resolution"] is True
    assert again["case"]["id"] == first["case"]["id"]
    assert again["case"]["receipt"] == first["case"]["receipt"]

    assert_exactly_one_180_refund(world)
    assert len(world.rows("select 1 from cases")) == 1


def test_a01_the_green_jacket_can_still_be_returned_as_its_own_action(world: World):
    world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline", line="blue"),
    )
    green = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline", line="green"),
    ).json()

    assert green["existing_resolution"] is False
    assert green["case"]["state"] == "resolved"
    assert green["case"]["amount_cents"] == 12000

    # Two legitimate returns on one order: two actions, two refunds, $300 in total.
    actions = world.actions("northline")
    assert sorted(a["amount_cents"] for a in actions) == [12000, 18000]
    assert len({a["idempotency_key"] for a in actions}) == 2
    refunds = world.processor.list_refunds(NORTHLINE_PAYMENT)
    assert sorted(r.amount_cents for r in refunds) == [12000, 18000]


def test_a02_lost_response_then_retry_and_restart(world: World):
    # The processor will accept the refund and then lose the response.
    world.processor.arm_fault("lost_response", payment_ref=NORTHLINE_PAYMENT)
    body = world.refund_request("northline")

    first = world.call("POST", "/cases", user="maya", merchant="northline", json_body=body)
    assert first.status_code == 200
    case = first.json()["case"]
    assert case["state"] == "reconciling"
    assert case["status"] == "pending_verification"
    assert case["receipt"] is None

    # In between: the processor has the refund, the application does not know yet, and the
    # units stay reserved so nothing else can claim them.
    assert len(world.processor.list_refunds(NORTHLINE_PAYMENT)) == 1
    assert world.ledger("northline") == []
    assert [a["status"] for a in world.actions("northline")] == ["reconciling"]
    blue = world.line("northline", "1001", "blue")
    assert (blue["reserved_quantity"], blue["reserved_cents"]) == (1, 18000)
    assert (blue["refunded_quantity"], blue["refunded_cents"]) == (0, 0)

    # The shopper refreshes (same client request) ...
    refresh = world.call("POST", "/cases", user="maya", merchant="northline", json_body=body).json()
    assert refresh["replayed"] is True
    assert refresh["case"]["id"] == case["id"]
    assert refresh["case"]["status"] == "pending_verification"

    # ... and retries from scratch (a new client request for the same jacket).
    retry = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    ).json()
    assert retry["existing_resolution"] is True
    assert retry["case"]["id"] == case["id"]
    assert retry["case"]["status"] == "pending_verification"
    assert len(world.actions("northline")) == 1
    assert len(world.processor.list_refunds(NORTHLINE_PAYMENT)) == 1

    # Reconciling before the retry is due does nothing.
    assert world.reconcile().json()["result"] == {"claimed": 0}

    # The worker restarts. Only committed state survives. Then the retry comes due.
    world.restart()
    world.clock.advance(seconds=world.settings.retry_base_seconds + 1)
    assert world.reconcile().json()["result"] == {"claimed": 1, "succeeded": 1}

    refund_id = assert_exactly_one_180_refund(world)
    assert_green_line_untouched(world)

    # The second attempt found the refund at the processor instead of sending a new one.
    attempts = world.rows("select attempt_no, operation, outcome from action_attempts order by id")
    assert [(a["attempt_no"], a["operation"], a["outcome"]) for a in attempts] == [
        (1, "create_refund", "unknown"),
        (2, "find_refund", "found"),
    ]
    creates = [
        r
        for r in world.processor.list_requests(NORTHLINE_PAYMENT)
        if r["operation"] == "create_refund"
    ]
    assert len(creates) == 1

    # With the browser long closed, the case is simply done when the shopper comes back.
    final = world.call("GET", f"/cases/{case['id']}", user="maya", merchant="northline").json()
    assert final["case"]["state"] == "resolved"
    assert final["case"]["receipt"]["refund_reference"] == refund_id

    # Nothing is left to do, however often reconcile runs.
    assert world.reconcile().json()["result"] == {"claimed": 0}
    assert_exactly_one_180_refund(world)


def test_a02_timeout_before_the_processor_recorded_anything(world: World):
    world.processor.arm_fault("timeout_before", payment_ref=NORTHLINE_PAYMENT)

    case = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    ).json()["case"]
    assert case["status"] == "pending_verification"
    assert world.processor.list_refunds(NORTHLINE_PAYMENT) == []

    world.clock.advance(seconds=world.settings.retry_base_seconds + 1)
    assert world.reconcile().json()["result"] == {"claimed": 1, "succeeded": 1}

    # Looked first, found nothing, then sent the refund once with the same key.
    attempts = world.rows("select attempt_no, operation, outcome from action_attempts order by id")
    assert [(a["attempt_no"], a["operation"], a["outcome"]) for a in attempts] == [
        (1, "create_refund", "unknown"),
        (2, "find_refund", "not_found"),
        (2, "create_refund", "succeeded"),
    ]
    keys = {
        r["idempotency_key"]
        for r in world.processor.list_requests(NORTHLINE_PAYMENT)
        if r["operation"] == "create_refund"
    }
    assert len(keys) == 1
    assert_exactly_one_180_refund(world)


def test_a02_repeated_lost_responses_never_create_a_second_refund(world: World):
    # The processor would lose the response to every create call it gets. Retries must not
    # depend on a create call ever answering.
    world.processor.arm_fault("lost_response", payment_ref=NORTHLINE_PAYMENT, times=5)
    case = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    ).json()["case"]

    for _ in range(3):
        world.restart()
        world.clock.advance(seconds=world.settings.retry_max_seconds + 1)
        world.reconcile()

    assert_exactly_one_180_refund(world)
    final = world.call("GET", f"/cases/{case['id']}", user="maya", merchant="northline").json()
    assert final["case"]["state"] == "resolved"


def test_a02_a_verified_failure_releases_the_reservation(world: World):
    world.processor.arm_fault("reject", payment_ref=NORTHLINE_PAYMENT)

    case = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    ).json()["case"]

    assert case["state"] == "declined"
    assert case["receipt"] is None
    assert world.processor.list_refunds(NORTHLINE_PAYMENT) == []
    assert world.ledger("northline") == []
    assert [a["status"] for a in world.actions("northline")] == ["failed"]
    # The balance is preserved: the blue jacket can be asked about again.
    blue = world.line("northline", "1001", "blue")
    assert (blue["reserved_quantity"], blue["reserved_cents"]) == (0, 0)
    assert (blue["refunded_quantity"], blue["refunded_cents"]) == (0, 0)


def test_a02_worker_dies_mid_call_and_another_takes_over_after_the_lease(world: World):
    """A worker claims the action, its call reaches the processor, and the process dies
    before recording anything. The lease keeps others away until it expires; then the next
    worker reconciles first and settles the one refund that exists."""
    from returns_desk.services import cases

    principal = world.principal("maya", "northline")
    data = cases.OpenCaseInput(**world.refund_request("northline"))
    # Authorize without the API's immediate attempt, as if the process died right after commit.
    result = cases.open_case(world.container.db, world.clock, principal, data, "req_test")
    assert result.action_id is not None
    action = world.actions("northline")[0]
    assert action["status"] == "pending"

    # The doomed worker: claims, calls the processor, then vanishes.
    claimed = world.container.execution._claim(result.action_id)
    assert claimed is not None
    world.processor.create_refund(
        payment_ref=NORTHLINE_PAYMENT,
        amount_cents=18000,
        idempotency_key=action["idempotency_key"],
        metadata={"action_id": str(action["id"])},
    )
    world.restart()

    # While the lease is held, nobody else may touch the action.
    assert world.reconcile().json()["result"] == {"claimed": 0}
    assert [a["status"] for a in world.actions("northline")] == ["pending"]

    world.clock.advance(seconds=world.settings.lease_seconds + 1)
    assert world.reconcile().json()["result"] == {"claimed": 1, "succeeded": 1}
    assert_exactly_one_180_refund(world)
