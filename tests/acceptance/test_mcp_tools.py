"""The MCP tool server over its real HTTP endpoint, called with the MCP SDK's own client.

Covers the tool contracts and the MCP half of A07: another shopper's order or another
tenant's case reveals nothing and changes nothing, exactly as over plain HTTP.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest

from returns_desk.seeds import Ids
from tests.conftest import World

pytestmark = pytest.mark.acceptance

NORTHLINE_PAYMENT = Ids.payment_ref("northline", "1001")
BLUE = str(Ids.line("northline", "1001", "blue"))
UNKNOWN = "00000000-0000-4000-8000-000000000000"


def error_of(result: Any) -> dict[str, Any]:
    assert result.is_error is True
    text = " ".join(block.text for block in result.content)
    return json.loads(text[text.index("{") :])


def open_shell_case(world: World, user: str = "maya", merchant: str = "northline") -> str:
    """A chat case with nothing decided yet, to hang tool calls on."""
    return world.say(user, merchant, "hello")["id"]


def test_the_endpoint_lists_exactly_the_agent_tools(world: World):
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    app = world.client.app
    headers = world.auth("maya", "northline")

    async def list_tools() -> list[Any]:
        async with (
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url="http://testserver",
                headers=headers,
            ) as http,
            Client(streamable_http_client("http://testserver/mcp", http_client=http)) as client,
        ):
            return (await client.list_tools()).tools

    tools = {tool.name: tool for tool in world.client.portal.call(list_tools)}
    assert sorted(tools) == [
        "check_inventory",
        "get_order",
        "get_resolution_status",
        "quote_resolution",
        "search_policy",
        "submit_resolution",
    ]
    # No tool accepts a merchant, a user, a role or an amount. Those come from the server.
    for tool in tools.values():
        properties = set(tool.input_schema["properties"])
        assert not properties & {"merchant_id", "user_id", "role", "amount_cents", "shopper_id"}
    # There is no way to decide a review from here.
    assert not any("review" in name or "approve" in name or "decision" in name for name in tools)


def test_quote_then_submit_over_mcp(world: World):
    case_id = open_shell_case(world)

    orders = world.mcp("maya", "northline", "get_order", case_id=case_id).structured_content
    assert [o["order_number"] for o in orders["orders"]] == ["NL-1001"]
    lines = {line["variant"]: line for line in orders["orders"][0]["lines"]}
    assert lines["Blue / M"]["remaining_cents"] == 18000
    assert lines["Blue / M"]["line_id"] == BLUE

    quote = world.mcp(
        "maya",
        "northline",
        "quote_resolution",
        case_id=case_id,
        order_line_id=BLUE,
        quantity=1,
        reason="damaged",
    ).structured_content
    assert quote["eligible"] is True
    assert quote["amount_cents"] == 18000
    assert quote["requires_review"] is False
    assert [c["section_key"] for c in quote["citations"]] == ["returns.window", "returns.damaged"]
    # A quote changes no financial state.
    assert world.processor.list_requests(NORTHLINE_PAYMENT) == []
    assert world.actions("northline") == []
    blue = world.line("northline", "1001", "blue")
    assert (blue["reserved_quantity"], blue["reserved_cents"]) == (0, 0)

    submitted = world.mcp(
        "maya", "northline", "submit_resolution", case_id=case_id, proposal_id=quote["proposal_id"]
    ).structured_content
    assert submitted["outcome"] == "processing"
    assert submitted["case_state"] == "resolved"

    status = world.mcp("maya", "northline", "get_resolution_status", case_id=case_id)
    status = status.structured_content
    assert status["status"] == "refunded"
    assert status["receipt"]["amount_cents"] == 18000
    refunds = world.processor.list_refunds(NORTHLINE_PAYMENT)
    assert status["receipt"]["refund_reference"] == refunds[0].id

    # Submitting again, or an older proposal, never pays twice.
    again = world.mcp(
        "maya", "northline", "submit_resolution", case_id=case_id, proposal_id=quote["proposal_id"]
    ).structured_content
    assert again["outcome"] in ("stale_proposal", "already_submitted")
    assert len(world.processor.list_refunds(NORTHLINE_PAYMENT)) == 1
    assert len(world.actions("northline")) == 1


def test_amounts_and_merchants_cannot_be_supplied_by_the_caller(world: World):
    case_id = open_shell_case(world)
    result = world.mcp(
        "maya",
        "northline",
        "quote_resolution",
        case_id=case_id,
        order_line_id=BLUE,
        quantity=1,
        reason="damaged",
        amount_cents=50000,
        merchant_id=str(Ids.merchant("vale")),
        role="supervisor",
    )
    # Unknown arguments are either refused or ignored. Either way the server's own numbers
    # stand and nothing has moved.
    if not result.is_error:
        assert result.structured_content["amount_cents"] == 18000
    assert world.processor.list_requests(NORTHLINE_PAYMENT) == []
    proposals = world.rows("select amount_cents, merchant_id from proposals")
    assert all(p["amount_cents"] == 18000 for p in proposals)
    assert all(p["merchant_id"] == Ids.merchant("northline") for p in proposals)


def test_review_is_required_over_mcp_too(world: World):
    case_id = open_shell_case(world, merchant="vale")
    vale_blue = str(Ids.line("vale", "1001", "blue"))
    quote = world.mcp(
        "maya",
        "vale",
        "quote_resolution",
        case_id=case_id,
        order_line_id=vale_blue,
        quantity=1,
        reason="damaged",
    ).structured_content
    assert quote["eligible"] is True and quote["requires_review"] is True
    # The reason for the review is internal and is not in the tool result.
    assert "limit" not in json.dumps(quote).lower()

    submitted = world.mcp(
        "maya", "vale", "submit_resolution", case_id=case_id, proposal_id=quote["proposal_id"]
    ).structured_content
    assert submitted["outcome"] == "pending_review"
    assert world.processor.list_requests(Ids.payment_ref("vale", "1001")) == []
    assert world.actions("vale") == []


def test_only_the_owning_shopper_can_submit(world: World):
    case_id = open_shell_case(world)
    quote = world.mcp(
        "maya",
        "northline",
        "quote_resolution",
        case_id=case_id,
        order_line_id=BLUE,
        quantity=1,
        reason="damaged",
    ).structured_content
    for user in ("nina", "sam"):
        refused = world.mcp(
            user,
            "northline",
            "submit_resolution",
            case_id=case_id,
            proposal_id=quote["proposal_id"],
        )
        assert error_of(refused)["code"] == "forbidden"
    assert world.processor.list_requests(NORTHLINE_PAYMENT) == []
    assert world.actions("northline") == []


def test_a07_over_mcp_other_peoples_orders_and_cases_look_unknown(world: World):
    mine = open_shell_case(world)  # Maya at Northline
    vale_case = world.say("maya", "vale", "hello")["id"]  # Maya at Vale
    omar_line = str(Ids.line("northline", "2001", "tote"))
    not_found = {"code": "not_found", "message": "Not found"}
    before = world.app_side_snapshot()

    # Another shopper's order line, quoted on my own case.
    theirs = world.mcp(
        "maya",
        "northline",
        "quote_resolution",
        case_id=mine,
        order_line_id=omar_line,
        quantity=1,
        reason="damaged",
    )
    made_up = world.mcp(
        "maya",
        "northline",
        "quote_resolution",
        case_id=mine,
        order_line_id=str(uuid4()),
        quantity=1,
        reason="damaged",
    )
    assert error_of(theirs) == error_of(made_up) == not_found

    # Another tenant's case, with every tool, from several callers.
    callers = [("maya", "northline"), ("omar", "vale"), ("sam", "northline"), ("nina", "northline")]
    for user, merchant in callers:
        calls = [
            ("get_order", {"case_id": vale_case}),
            ("search_policy", {"case_id": vale_case, "question": "returns"}),
            ("get_resolution_status", {"case_id": vale_case}),
            (
                "quote_resolution",
                {
                    "case_id": vale_case,
                    "order_line_id": str(Ids.line("vale", "1001", "blue")),
                    "quantity": 1,
                    "reason": "damaged",
                },
            ),
            ("submit_resolution", {"case_id": vale_case, "proposal_id": UNKNOWN}),
        ]
        for tool, arguments in calls:
            seen = world.mcp(user, merchant, tool, **arguments)
            unknown = world.mcp(user, merchant, tool, **{**arguments, "case_id": UNKNOWN})
            assert error_of(seen) == error_of(unknown) == not_found, (user, merchant, tool)

    # Malformed references are treated like unknown ones.
    assert error_of(world.mcp("maya", "northline", "get_order", case_id="not-a-uuid")) == not_found

    # No token, or a bad one: nothing.
    assert error_of(world.mcp(None, None, "get_order", case_id=mine))["code"] == "not_authenticated"

    # Order lookups only ever show my own orders.
    orders = world.mcp("maya", "northline", "get_order", case_id=mine).structured_content
    assert [o["order_number"] for o in orders["orders"]] == ["NL-1001"]
    by_number = world.mcp(
        "maya", "northline", "get_order", case_id=mine, order_number="NL-2001"
    ).structured_content
    assert by_number["orders"] == []

    # No disclosed details, no provider call, no mutation.
    assert world.app_side_snapshot() == before
    assert world.processor.list_requests() == []
    assert (
        world.rows(
            "select 1 from order_lines where reserved_quantity <> 0 or refunded_quantity <> 0"
        )
        == []
    )


def test_a_quote_for_units_in_another_request_points_at_that_request(world: World):
    first = world.say(
        "maya",
        "northline",
        "I ordered two jackets. The blue one arrived damaged. Please refund just that one.",
    )
    case_id = open_shell_case(world)
    quote = world.mcp(
        "maya",
        "northline",
        "quote_resolution",
        case_id=case_id,
        order_line_id=BLUE,
        quantity=1,
        reason="damaged",
    ).structured_content
    assert quote["eligible"] is False
    assert quote["existing_case_id"] == first["id"]
    assert "quantity_unavailable" in quote["reason_codes"]
    assert len(world.processor.list_refunds(NORTHLINE_PAYMENT)) == 1
