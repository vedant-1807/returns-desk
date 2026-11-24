"""A13: partial returns and payment provider events.

Given two partial returns of different units, plus duplicate and out-of-order payment
events: allocations sum correctly, pending units cannot be reused, and each action settles
once.

Events arrive at POST /webhooks/payments, signed the way Stripe signs them. Nothing is
trusted until the signature over the raw body checks out.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from returns_desk.processor import webhooks
from returns_desk.seeds import Ids
from tests.conftest import World

pytestmark = pytest.mark.acceptance

PAYMENT = Ids.payment_ref("northline", "1300")
UNIT = 2500  # three pairs of socks at $25 each on one order line


def return_socks(world: World, quantity: int, request_id: str) -> dict[str, Any]:
    body = world.refund_request(
        "northline", number="1300", line="socks", quantity=quantity, client_request_id=request_id
    )
    response = world.call("POST", "/cases", user="maya", merchant="northline", json_body=body)
    assert response.status_code == 200, response.text
    return response.json()


def deliver(
    world: World,
    event: dict[str, Any],
    *,
    secret: str | None = None,
    timestamp: int | None = None,
    signature: str | None = None,
) -> Any:
    payload = webhooks.encode(event)
    header = signature or webhooks.sign(
        payload,
        secret or world.settings.payment_webhook_secret,
        timestamp if timestamp is not None else int(world.clock.now().timestamp()),
    )
    return world.client.post(
        "/webhooks/payments",
        content=payload,
        headers={"Stripe-Signature": header, "Content-Type": "application/json"},
    )


def socks(world: World) -> dict[str, Any]:
    return world.line("northline", "1300", "socks")


def refund_of(world: World, case_id: str) -> Any:
    [action] = world.rows("select id from resolution_actions where case_id = %s", (case_id,))
    [refund] = [
        r
        for r in world.processor.list_refunds(PAYMENT)
        if r.metadata["action_id"] == str(action["id"])
    ]
    return refund


@pytest.mark.seed("multi_unit")
def test_a13_two_partial_returns_with_repeated_and_out_of_order_events(world: World):
    # Both refunds reach the processor, and both answers are lost on the way back.
    world.processor.arm_fault("lost_response", payment_ref=PAYMENT, times=2)
    first = return_socks(world, 1, "socks-1")["case"]
    second = return_socks(world, 1, "socks-2")["case"]

    # Two separate requests, two actions, both waiting on the provider.
    assert first["id"] != second["id"]
    assert (first["state"], second["state"]) == ("reconciling", "reconciling")
    line = socks(world)
    assert (line["reserved_quantity"], line["reserved_cents"]) == (2, 2 * UNIT)
    assert (line["refunded_quantity"], line["refunded_cents"]) == (0, 0)

    # Pending units cannot be used again. Two of three are held, so a request for two
    # more is pointed at a request that already exists and starts nothing new.
    greedy = return_socks(world, 2, "socks-greedy")
    assert greedy["existing_resolution"] is True
    assert len(world.actions("northline", "1300")) == 2
    assert socks(world)["reserved_quantity"] == 2

    r1, r2 = refund_of(world, first["id"]), refund_of(world, second["id"])

    # The second refund's news arrives first.
    settled = deliver(world, world.processor.refund_event(r2.id))
    assert settled.status_code == 200
    assert settled.json() == {"received": True, "duplicate": False, "outcome": "settled"}
    line = socks(world)
    assert (line["reserved_quantity"], line["refunded_quantity"]) == (1, 1)

    # An older event about the same refund turns up late. The result stands.
    stale = deliver(world, world.processor.refund_event(r2.id, status="pending"))
    assert stale.json()["outcome"] == "not_final_yet"
    # The same event is delivered again. It is recognised and changes nothing.
    repeat = deliver(world, world.processor.refund_event(r2.id))
    assert repeat.json() == {"received": True, "duplicate": True, "outcome": "settled"}

    # The first refund's event, twice in a row.
    for expected_duplicate in (False, True):
        response = deliver(world, world.processor.refund_event(r1.id))
        assert response.json()["duplicate"] is expected_duplicate
        assert response.json()["outcome"] == "settled"

    # Each action settled once, and the allocations add up on both sides.
    actions = world.actions("northline", "1300")
    assert [(a["status"], a["amount_cents"], a["quantity"]) for a in actions] == [
        ("succeeded", UNIT, 1),
        ("succeeded", UNIT, 1),
    ]
    assert {a["provider_refund_id"] for a in actions} == {r1.id, r2.id}
    ledger = world.ledger("northline", "1300")
    assert sorted(entry["provider_refund_id"] for entry in ledger) == sorted([r1.id, r2.id])
    assert sum(entry["amount_cents"] for entry in ledger) == 2 * UNIT
    assert sum(r.amount_cents for r in world.processor.list_refunds(PAYMENT)) == 2 * UNIT
    line = socks(world)
    assert (line["reserved_quantity"], line["reserved_cents"]) == (0, 0)
    assert (line["refunded_quantity"], line["refunded_cents"]) == (2, 2 * UNIT)
    settlements = world.rows(
        "select action_id, count(*) as n from action_attempts "
        "where operation = 'provider_event' and outcome = 'succeeded' group by action_id"
    )
    assert sorted(s["n"] for s in settlements) == [1, 1]
    for case_id in (first["id"], second["id"]):
        view = world.call("GET", f"/cases/{case_id}", user="maya", merchant="northline").json()
        assert view["case"]["state"] == "resolved"
        assert view["case"]["receipt"]["amount_cents"] == UNIT

    # The reconcile job finds nothing left to do and sends nothing to the processor.
    calls_before = len(world.processor.list_requests(PAYMENT))
    assert world.reconcile().json()["result"] == {"claimed": 0}
    assert len(world.processor.list_requests(PAYMENT)) == calls_before

    # One unit was never part of any of this, and can still be returned.
    last = return_socks(world, 1, "socks-3")["case"]
    assert last["state"] == "resolved"
    assert socks(world)["refunded_cents"] == 3 * UNIT
    world.save_evidence(
        payment_events=world.rows(
            "select event_id, event_type, refund_status, outcome from payment_events order by id"
        )
    )


@pytest.mark.seed("multi_unit")
def test_an_event_and_the_reconcile_job_cannot_both_settle_one_refund(world: World):
    world.processor.arm_fault("lost_response", payment_ref=PAYMENT)
    case = return_socks(world, 1, "socks-1")["case"]
    refund = refund_of(world, case["id"])

    # The retry is due at the same time the event arrives. Whoever comes second sees a
    # final result and leaves it alone.
    world.clock.advance(minutes=10)
    assert world.reconcile().json()["result"]["succeeded"] == 1
    late = deliver(world, world.processor.refund_event(refund.id))

    assert late.json()["outcome"] == "already_final"
    assert len(world.ledger("northline", "1300")) == 1
    assert socks(world)["refunded_cents"] == UNIT


@pytest.mark.seed("multi_unit")
def test_a_verified_failure_event_releases_the_units(world: World):
    # The call timed out before the processor recorded anything.
    world.processor.arm_fault("timeout_before", payment_ref=PAYMENT)
    case = return_socks(world, 1, "socks-1")["case"]
    assert case["state"] == "reconciling"
    assert socks(world)["reserved_quantity"] == 1
    [action] = world.actions("northline", "1300")

    failed = {
        "id": "evt_sbx_failed_1",
        "type": "refund.failed",
        "created": int(world.clock.now().timestamp()),
        "account": "northline",
        "data": {
            "object": {
                "id": "re_sbx_never_paid",
                "object": "refund",
                "amount": UNIT,
                "status": "failed",
                "failure_reason": "expired_or_canceled_card",
                "payment_intent": PAYMENT,
                "metadata": {"action_id": str(action["id"])},
            }
        },
    }
    response = deliver(world, failed)

    # Only now, on a verified failure, is the reservation released.
    assert response.json()["outcome"] == "settled_failure"
    line = socks(world)
    assert (line["reserved_quantity"], line["reserved_cents"], line["refunded_cents"]) == (0, 0, 0)
    [action] = world.actions("northline", "1300")
    assert (action["status"], action["last_error"]) == ("failed", "expired_or_canceled_card")
    assert world.ledger("northline", "1300") == []

    # A success claim that turns up afterwards cannot overwrite the final result.
    forged_success = {**failed, "id": "evt_sbx_late_success", "type": "refund.updated"}
    forged_success["data"] = {"object": {**failed["data"]["object"], "status": "succeeded"}}
    assert deliver(world, forged_success).json()["outcome"] == "conflicts_with_final"
    assert world.ledger("northline", "1300") == []


@pytest.mark.seed("multi_unit")
def test_events_that_are_not_authentic_or_not_ours_change_nothing(world: World):
    world.processor.arm_fault("lost_response", payment_ref=PAYMENT)
    case = return_socks(world, 1, "socks-1")["case"]
    refund = refund_of(world, case["id"])
    event = world.processor.refund_event(refund.id)
    now = int(world.clock.now().timestamp())

    # Wrong secret, no signature, a body changed after signing, and a signature too old.
    assert deliver(world, event, secret="not-the-secret").status_code == 400
    assert (
        world.client.post("/webhooks/payments", content=webhooks.encode(event)).status_code == 400
    )
    tampered = {**event, "data": {"object": {**event["data"]["object"], "amount": 1}}}
    signed_for_original = webhooks.sign(
        webhooks.encode(event), world.settings.payment_webhook_secret, now
    )
    assert deliver(world, tampered, signature=signed_for_original).status_code == 400
    assert deliver(world, event, timestamp=now - 3600).status_code == 400
    assert world.rows("select 1 from payment_events") == []

    # Authentic, but for another merchant's account, for the wrong amount, or for an
    # action that does not exist. Stored for the record, applied to nothing.
    elsewhere = {**event, "id": "evt_other_account", "account": "vale"}
    wrong_amount = {
        **event,
        "id": "evt_wrong_amount",
        "data": {"object": {**event["data"]["object"], "amount": UNIT * 3}},
    }
    unknown = {
        **event,
        "id": "evt_unknown",
        "data": {
            "object": {
                **event["data"]["object"],
                "metadata": {"action_id": "00000000-0000-0000-0000-000000000000"},
            }
        },
    }
    not_a_refund = {
        "id": "evt_charge",
        "type": "charge.succeeded",
        "created": now,
        "data": {"object": {"id": "ch_1", "object": "charge"}},
    }
    outcomes = [
        deliver(world, e).json()["outcome"]
        for e in (elsewhere, wrong_amount, unknown, not_a_refund)
    ]
    assert outcomes == ["account_mismatch", "receipt_mismatch", "unknown_action", "ignored_type"]

    [action] = world.actions("northline", "1300")
    assert action["status"] == "reconciling"
    assert socks(world)["reserved_quantity"] == 1
    assert world.ledger("northline", "1300") == []

    # The real event still works afterwards.
    assert deliver(world, event).json()["outcome"] == "settled"


def test_the_signature_check_itself():
    payload = b'{"id":"evt_1"}'
    now_seconds = int(time.time())
    from datetime import UTC, datetime

    now = datetime.fromtimestamp(now_seconds, UTC)
    good = webhooks.sign(payload, "whsec_test", now_seconds)

    webhooks.verify(payload, good, "whsec_test", now)
    # Stripe sends several v1 values while a secret is being rolled. Any one may match.
    webhooks.verify(payload, good + ",v1=" + "0" * 64, "whsec_test", now)
    for header in (None, "", "t=abc,v1=xyz", "v1=" + "0" * 64, f"t={now_seconds}"):
        with pytest.raises(webhooks.InvalidSignature):
            webhooks.verify(payload, header, "whsec_test", now)
    with pytest.raises(webhooks.InvalidSignature):
        webhooks.verify(payload, good, "", now)
