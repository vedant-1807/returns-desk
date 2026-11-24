"""Exchanges and inventory, through the typed API.

A08  Two simultaneous exchanges compete for one remaining unit: one reservation and one
     fulfillment, stock never negative, and the other shopper gets a choice.
A09  A refund and an exchange race for the same final unit: at most one action consumes
     it, and the remaining quantity and amount reconcile.

An exchange moves no money. In one transaction it takes the replacement out of stock, uses
up the order line's returnable unit and writes one fulfillment record.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import psycopg
import pytest

from returns_desk.seeds import Ids
from tests.conftest import World

pytestmark = pytest.mark.acceptance

NORTHLINE_PAYMENT = Ids.payment_ref("northline", "1001")


def exchange(
    world: World,
    merchant: str,
    *,
    number: str = "1001",
    line: str = "blue",
    replacement: str = "Green / M",
    consent: bool = True,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    body = world.refund_request(
        merchant, number=number, line=line, consent=consent, client_request_id=client_request_id
    )
    body.update(
        action="exchange",
        replacement_variant=replacement,
        message=f"It arrived damaged. Please exchange it for {replacement}.",
    )
    return body


def stock(world: World, merchant: str, variant: str, sku: str = "JKT-TRAIL") -> int:
    return world.rows(
        "select available from inventory where merchant_id = %s and sku = %s and variant = %s",
        (Ids.merchant(merchant), sku, variant),
    )[0]["available"]


def fulfillments(world: World) -> list[dict[str, Any]]:
    return world.rows("select * from fulfillments order by created_at, id")


def units(world: World, merchant: str, number: str, key: str) -> tuple[int, int, int, int]:
    """(reserved, refunded, exchanged, refunded cents) of one order line."""
    row = world.line(merchant, number, key)
    return (
        row["reserved_quantity"],
        row["refunded_quantity"],
        row["exchanged_quantity"],
        row["refunded_cents"],
    )


# --- one exchange -----------------------------------------------------------------------


def test_an_exchange_takes_stock_uses_the_unit_and_writes_one_fulfillment(world: World):
    response = world.call(
        "POST", "/cases", user="maya", merchant="northline", json_body=exchange(world, "northline")
    )

    assert response.status_code == 200
    case = response.json()["case"]
    assert (case["state"], case["status"]) == ("resolved", "exchanged")
    assert case["requested_action"] == "exchange"
    assert case["amount_cents"] is None  # no money moves
    assert case["receipt"] is None
    assert case["exchange"]["replacement_variant"] == "Green / M"
    reference = case["exchange"]["fulfillment"]["reference"]
    assert reference.startswith("FUL-")
    assert reference in case["messages"][-1]["body"]

    # Stock, the order line, the action and the fulfillment all moved together, once.
    assert stock(world, "northline", "Green / M") == 2
    assert units(world, "northline", "1001", "blue") == (0, 0, 1, 0)
    [action] = world.actions("northline")
    assert (action["kind"], action["status"], action["amount_cents"]) == (
        "exchange",
        "succeeded",
        0,
    )
    [fulfillment] = fulfillments(world)
    assert (fulfillment["variant"], fulfillment["quantity"]) == ("Green / M", 1)
    assert fulfillment["action_id"] == action["id"]

    # No payment was involved, and the other jacket and the other merchant are untouched.
    assert world.processor.list_requests() == []
    assert world.ledger("northline") == []
    assert units(world, "northline", "1001", "green") == (0, 0, 0, 0)
    assert stock(world, "vale", "Green / M") == 3


def test_an_exchanged_unit_cannot_also_be_refunded(world: World):
    world.call(
        "POST", "/cases", user="maya", merchant="northline", json_body=exchange(world, "northline")
    )

    again = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    )

    # The unit is spoken for: the shopper is pointed at the exchange, and nothing is paid.
    assert again.status_code == 200
    assert again.json()["existing_resolution"] is True
    assert again.json()["case"]["status"] == "exchanged"
    assert world.processor.list_requests(NORTHLINE_PAYMENT) == []
    assert len(world.actions("northline")) == 1


def test_an_exchange_above_the_limit_waits_for_review_and_holds_no_stock(world: World):
    # $180 of goods is above Vale's $150 line, exchange or refund.
    response = world.call(
        "POST", "/cases", user="maya", merchant="vale", json_body=exchange(world, "vale")
    )
    case = response.json()["case"]

    assert case["state"] == "awaiting_supervisor"
    assert stock(world, "vale", "Green / M") == 3
    assert fulfillments(world) == []

    [review] = world.call("GET", "/reviews", user="vera", merchant="vale").json()["reviews"]
    assert review["proposed_action"] == "exchange"
    assert review["replacement_variant"] == "Green / M"
    assert review["reasons"] == ["above_auto_limit"]

    decided = world.call(
        "POST",
        f"/reviews/{review['review_id']}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "approve", "note": "Fine."},
    )

    assert decided.status_code == 200
    assert decided.json()["case"]["state"] == "resolved"
    assert decided.json()["case"]["exchange"]["fulfillment"]["variant"] == "Green / M"
    assert stock(world, "vale", "Green / M") == 2
    assert len(fulfillments(world)) == 1
    assert world.processor.list_requests() == []

    # A second click on approve changes nothing.
    again = world.call(
        "POST",
        f"/reviews/{review['review_id']}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "approve", "note": ""},
    )
    assert again.json()["decision"]["already_decided"] is True
    assert stock(world, "vale", "Green / M") == 2
    assert len(fulfillments(world)) == 1


def test_a_declined_exchange_leaves_stock_and_the_order_alone(world: World):
    world.call("POST", "/cases", user="maya", merchant="vale", json_body=exchange(world, "vale"))
    [review] = world.call("GET", "/reviews", user="vera", merchant="vale").json()["reviews"]

    world.call(
        "POST",
        f"/reviews/{review['review_id']}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "decline", "note": "No photo."},
    )

    assert stock(world, "vale", "Green / M") == 3
    assert fulfillments(world) == []
    assert units(world, "vale", "1001", "blue") == (0, 0, 0, 0)


def test_the_replacement_sells_out_while_a_review_waits(world: World):
    world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="vale",
        json_body=exchange(world, "vale", replacement="Blue / L"),
    )
    [review] = world.call("GET", "/reviews", user="vera", merchant="vale").json()["reviews"]
    with world.admin() as conn:
        conn.execute(
            "update inventory set available = 0 where merchant_id = %s and variant = 'Blue / L'",
            (Ids.merchant("vale"),),
        )

    decided = world.call(
        "POST",
        f"/reviews/{review['review_id']}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "approve", "note": "Fine."},
    )

    # Nothing is approved and nothing is declined. The choice goes back to the shopper.
    assert decided.status_code == 409
    assert decided.json()["error"]["details"]["hard_constraint"] == "replacement_unavailable"
    assert world.rows("select 1 from approvals") == []
    assert fulfillments(world) == []
    case = world.call("GET", f"/cases/{review['case_id']}", user="maya", merchant="vale").json()
    assert case["case"]["state"] == "awaiting_customer"
    assert "not available" in case["case"]["messages"][-1]["body"]
    assert world.call("GET", "/reviews", user="vera", merchant="vale").json()["reviews"] == []


@pytest.mark.parametrize("wanted", ["Blue / S", "Purple / XL"])
def test_a_replacement_that_cannot_be_had_gets_a_choice_not_a_refusal(world: World, wanted: str):
    response = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=exchange(world, "northline", replacement=wanted),
    )
    case = response.json()["case"]

    assert case["state"] == "awaiting_customer"
    offer = case["messages"][-1]["body"]
    assert "not available" in offer
    assert (
        "Blue / L" in offer
        and "Green / M" in offer
        and "Blue / S" not in offer.split("In stock:")[1]
    )
    assert fulfillments(world) == []
    assert world.actions("northline") == []
    assert units(world, "northline", "1001", "blue") == (0, 0, 0, 0)

    # The shopper picks one that is in stock and confirms.
    chosen = world.call(
        "POST",
        f"/cases/{case['id']}/messages",
        user="maya",
        merchant="northline",
        json_body={
            "client_request_id": "pick-another",
            "replacement_variant": "Green / L",
            "consent": True,
        },
    )
    assert chosen.json()["case"]["status"] == "exchanged"
    assert stock(world, "northline", "Green / L") == 1


def test_an_exchange_request_is_validated(world: World):
    missing = exchange(world, "northline")
    del missing["replacement_variant"]
    stray = world.refund_request("northline")
    stray["replacement_variant"] = "Green / M"

    for body in (missing, stray):
        response = world.call("POST", "/cases", user="maya", merchant="northline", json_body=body)
        assert response.status_code == 422
    assert world.rows("select 1 from cases") == []


# --- A08 --------------------------------------------------------------------------------


@pytest.mark.seed("last_unit")
def test_a08_two_exchanges_compete_for_the_last_unit(world: World):
    assert stock(world, "northline", "Blue / L") == 1
    requests = [
        ("maya", exchange(world, "northline", replacement="Blue / L")),
        ("omar", exchange(world, "northline", number="2002", line="green", replacement="Blue / L")),
    ]

    def send(who_and_body: tuple[str, dict[str, Any]]) -> dict[str, Any]:
        who, body = who_and_body
        response = world.call("POST", "/cases", user=who, merchant="northline", json_body=body)
        assert response.status_code == 200, response.text
        return {"who": who, **response.json()["case"]}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, requests))

    # One reservation and one fulfillment. Stock is at zero, not below.
    won = [r for r in results if r["state"] == "resolved"]
    lost = [r for r in results if r["state"] != "resolved"]
    assert len(won) == 1 and len(lost) == 1
    assert stock(world, "northline", "Blue / L") == 0
    [fulfillment] = fulfillments(world)
    assert str(fulfillment["case_id"]) == won[0]["id"] and fulfillment["variant"] == "Blue / L"
    exchange_actions = world.rows("select status from resolution_actions where kind = 'exchange'")
    assert [a["status"] for a in exchange_actions] == ["succeeded"]

    # The other shopper keeps their item and is given a choice.
    loser = lost[0]
    assert loser["state"] == "awaiting_customer"
    offer = loser["messages"][-1]["body"]
    assert "not available" in offer and "Green / L" in offer
    number, key = ("1001", "blue") if loser["who"] == "maya" else ("2002", "green")
    assert units(world, "northline", number, key) == (0, 0, 0, 0)

    # They take another one. Their exchange goes through; the first is not disturbed.
    chosen = world.call(
        "POST",
        f"/cases/{loser['id']}/messages",
        user=loser["who"],
        merchant="northline",
        json_body={
            "client_request_id": "second-choice",
            "replacement_variant": "Green / L",
            "consent": True,
        },
    )
    assert chosen.json()["case"]["status"] == "exchanged"
    assert stock(world, "northline", "Green / L") == 1
    assert stock(world, "northline", "Blue / L") == 0
    assert len(fulfillments(world)) == 2
    assert world.rows("select 1 from inventory where available < 0") == []


# --- A09 --------------------------------------------------------------------------------


def assert_one_unit_was_consumed_once(world: World) -> str:
    """The invariants of A09. Returns which action won: 'refund' or 'exchange'."""
    reserved, refunded, exchanged, refunded_cents = units(world, "northline", "1001", "blue")
    assert reserved == 0 and refunded + exchanged == 1
    actions = world.actions("northline")
    assert len(actions) == 1
    winner = actions[0]["kind"]
    refunds = world.processor.list_refunds(NORTHLINE_PAYMENT)
    if winner == "refund":
        assert (refunded, exchanged, refunded_cents) == (1, 0, 18000)
        assert [r.amount_cents for r in refunds] == [18000]
        assert fulfillments(world) == [] and stock(world, "northline", "Green / M") == 3
    else:
        assert (refunded, exchanged, refunded_cents) == (0, 1, 0)
        assert refunds == [] and world.ledger("northline") == []
        assert len(fulfillments(world)) == 1 and stock(world, "northline", "Green / M") == 2
    return winner


def test_a09_a_refund_and_an_exchange_race_for_the_same_final_unit(world: World):
    bodies = [world.refund_request("northline"), exchange(world, "northline")]

    def send(body: dict[str, Any]) -> dict[str, Any]:
        response = world.call("POST", "/cases", user="maya", merchant="northline", json_body=body)
        assert response.status_code == 200, response.text
        return response.json()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, bodies))

    winner = assert_one_unit_was_consumed_once(world)
    # Both callers are shown the one resolution that exists. Exactly one of them made it.
    assert sorted(r["existing_resolution"] for r in results) == [False, True]
    assert {r["case"]["id"] for r in results} == {results[0]["case"]["id"]}
    assert results[0]["case"]["status"] == ("refunded" if winner == "refund" else "exchanged")


@pytest.mark.parametrize("first", ["refund", "exchange"])
def test_a09_whichever_comes_first_wins_and_the_other_changes_nothing(world: World, first: str):
    bodies = {"refund": world.refund_request("northline"), "exchange": exchange(world, "northline")}
    second = "exchange" if first == "refund" else "refund"

    won = world.call("POST", "/cases", user="maya", merchant="northline", json_body=bodies[first])
    lost = world.call("POST", "/cases", user="maya", merchant="northline", json_body=bodies[second])

    assert won.json()["existing_resolution"] is False
    assert lost.json()["existing_resolution"] is True
    assert lost.json()["case"]["id"] == won.json()["case"]["id"]
    assert assert_one_unit_was_consumed_once(world) == first


# --- the database holds the line too --------------------------------------------------------


def test_the_database_itself_refuses_negative_stock_and_a_self_settled_refund(world: World):
    maya = world.principal("maya", "northline")
    created = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline", consent=False),
    ).json()["case"]
    proposal = world.rows("select * from proposals where case_id = %s", (created["id"],))[0]

    # Even with the API's own database role, stock cannot be driven below zero...
    with pytest.raises(psycopg.errors.CheckViolation), world.container.db.tenant_tx(maya) as conn:
        conn.execute("update inventory set available = -1 where variant = 'Blue / L'")

    # ...and a refund cannot be written as already paid. Only an exchange is done in place.
    with (
        pytest.raises(psycopg.errors.InsufficientPrivilege),
        world.container.db.tenant_tx(maya) as conn,
    ):
        conn.execute(
            """
            insert into resolution_actions
              (id, merchant_id, case_id, proposal_id, kind, order_id, order_line_id, quantity,
               amount_cents, provider_payment_ref, idempotency_key, status, created_at, updated_at)
            values (%s, %s, %s, %s, 'refund', %s, %s, 1, 18000, 'x', 'forged', 'succeeded',
                    now(), now())
            """,
            (
                proposal["action_id"],
                maya.merchant_id,
                created["id"],
                proposal["id"],
                Ids.order("northline", "1001"),
                Ids.line("northline", "1001", "blue"),
            ),
        )
    assert world.actions("northline") == []


def test_check_inventory_reads_only_the_callers_merchant(world: World):
    with world.admin() as conn:
        conn.execute(
            "update inventory set available = 9 where merchant_id = %s and variant = 'Blue / L'",
            (Ids.merchant("vale"),),
        )

    here = world.mcp("maya", "northline", "check_inventory", sku="JKT-TRAIL")
    there = world.mcp("maya", "vale", "check_inventory", sku="JKT-TRAIL", variant="Blue / L")
    nobody = world.mcp(None, None, "check_inventory", sku="JKT-TRAIL")

    by_variant = {v["variant"]: v for v in here.structured_content["variants"]}
    assert by_variant["Blue / L"] == {"variant": "Blue / L", "available": 1, "in_stock": True}
    assert by_variant["Blue / S"]["in_stock"] is False
    assert [v["available"] for v in there.structured_content["variants"]] == [9]
    assert nobody.is_error
