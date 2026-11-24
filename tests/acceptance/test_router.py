"""The router: when the stronger model is asked, and what it may settle.

The low-cost model answers first. The stronger one is asked only when that answer settled
nothing, or when it names a different item than the shopper's own words do. Whatever either
model says, an item is acted on only when the shopper's words identify it.
"""

from __future__ import annotations

from typing import Any

from returns_desk.models import keyword
from returns_desk.models.gateway import ModelRequest
from returns_desk.seeds import Ids
from tests.conftest import World
from tests.fakes import ModelUpstreams

REFERENCE = "I ordered two jackets. The blue one arrived damaged. Please refund just that one."
NORTHLINE_PAYMENT = Ids.payment_ref("northline", "1001")


def picks(line: str):
    """A model that reads the message correctly except for the item, which it gets wrong."""

    def write(request: ModelRequest) -> dict[str, Any]:
        return {**keyword.handle(request), "lines": [line]}

    return write


def routed_world(world: World, upstreams: ModelUpstreams) -> None:
    world.model = upstreams.gateway(strong_models=True)  # type: ignore[assignment]
    world.restart()


def test_a_clear_request_never_reaches_the_stronger_model(world: World):
    upstreams = ModelUpstreams(primary="ok")
    routed_world(world, upstreams)

    case = world.say("maya", "northline", REFERENCE)

    assert case["state"] == "resolved"
    assert upstreams.models_called == ["primary-low"]


def test_an_item_that_conflicts_with_the_shoppers_words_is_escalated(world: World):
    upstreams = ModelUpstreams(primary="ok")
    upstreams.answer_as["primary-low"] = picks("L2")  # green, but the shopper said blue
    routed_world(world, upstreams)

    case = world.say("maya", "northline", REFERENCE)

    # The stronger model agrees with the shopper's words, so the blue jacket is refunded.
    assert upstreams.models_called == ["primary-low", "primary-strong"]
    assert case["state"] == "resolved"
    assert case["item"]["variant"] == "Blue / M"
    assert [r.amount_cents for r in world.processor.list_refunds(NORTHLINE_PAYMENT)] == [18000]
    tiers = world.rows("select tier, outcome from model_attempts order by id")
    assert [(t["tier"], t["outcome"]) for t in tiers] == [("low", "ok"), ("strong", "ok")]


def test_when_the_stronger_model_also_disagrees_the_shopper_is_asked(world: World):
    upstreams = ModelUpstreams(primary="ok")
    upstreams.answer_as["primary-low"] = picks("L2")
    upstreams.answer_as["primary-strong"] = picks("L2")
    routed_world(world, upstreams)

    case = world.say("maya", "northline", REFERENCE)

    # Two models say green, the shopper's words say blue. Nobody guesses: one question.
    assert case["state"] == "awaiting_customer"
    assert case["next_action"] == "reply"
    assert world.processor.list_requests(NORTHLINE_PAYMENT) == []
    assert world.actions("northline") == []
