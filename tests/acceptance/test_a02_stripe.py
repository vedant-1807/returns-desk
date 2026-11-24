"""A02 again, with Stripe as the payment processor.

The same scenario as the sandbox A02: the processor accepts the refund, the response is
lost, the worker restarts and retries, and both sides end with exactly one $180 refund.

It runs two ways:

* always, against a stateful fake of Stripe's API (tests/fakes.py), and
* against real Stripe test mode when RD_STRIPE_TEST_KEY holds an sk_test_ key:

      RD_STRIPE_TEST_KEY=sk_test_... uv run pytest tests/acceptance/test_a02_stripe.py

In both, the request really reaches the far end and only the answer is dropped on the way
back, so the retry has to find the refund Stripe already made.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest

from returns_desk.config import Settings
from returns_desk.processor import webhooks
from returns_desk.processor.base import ProcessorRejected, ProcessorTimeout
from returns_desk.processor.stripe import StripeProcessor
from returns_desk.seeds import Ids
from tests.conftest import World
from tests.fakes import FakeStripe, LoseResponses

pytestmark = pytest.mark.acceptance

LIVE_KEY = os.environ.get("RD_STRIPE_TEST_KEY", "")


@dataclass
class StripeWorld:
    world: World
    lossy: LoseResponses
    fake: FakeStripe | None

    @property
    def processor(self) -> StripeProcessor:
        processor = self.world.container.processor
        assert isinstance(processor, StripeProcessor)
        return processor

    def payment_ref(self, merchant: str = "northline", number: str = "1001") -> str:
        return self.world.rows(
            "select provider_payment_ref from orders where id = %s",
            (Ids.order(merchant, number),),
        )[0]["provider_payment_ref"]


@pytest.fixture(
    params=[
        "fake",
        pytest.param(
            "live",
            marks=pytest.mark.skipif(
                not LIVE_KEY.startswith("sk_test_"),
                reason="set RD_STRIPE_TEST_KEY to an sk_test_ key to run against Stripe",
            ),
        ),
    ]
)
def stripe(request: pytest.FixtureRequest, settings: Settings) -> Iterator[StripeWorld]:
    fake = FakeStripe() if request.param == "fake" else None
    inner: httpx2.BaseTransport = fake.transport() if fake else httpx2.HTTPTransport()
    key = FakeStripe.KEY if fake else LIVE_KEY
    lossy = LoseResponses(inner)
    stripe_settings = settings.model_copy(update={"payment_provider": "stripe"})
    world = World(
        stripe_settings,
        "two_jackets",
        request.node.name,
        processor_factory=lambda: StripeProcessor(key, transport=lossy),
    )
    try:
        yield StripeWorld(world, lossy, fake)
        world.save_evidence(stripe_mode=request.param)
    finally:
        world.close()
        inner.close()


def test_a02_lost_response_then_retry_and_restart_with_stripe(stripe: StripeWorld):
    world = stripe.world
    payment = stripe.payment_ref()
    assert payment.startswith("pi_")
    # Stripe will make the refund, and the answer will be lost on the way back.
    stripe.lossy.lose_next.append(("POST", "/v1/refunds"))
    body = world.refund_request("northline")

    first = world.call("POST", "/cases", user="maya", merchant="northline", json_body=body)
    case = first.json()["case"]
    assert (case["state"], case["status"]) == ("reconciling", "pending_verification")
    assert case["receipt"] is None

    # In between: Stripe has the refund, the application does not know, the unit is held.
    assert [r.amount_cents for r in stripe.processor.list_refunds(payment)] == [18000]
    assert world.ledger("northline") == []
    blue = world.line("northline", "1001", "blue")
    assert (blue["reserved_quantity"], blue["reserved_cents"]) == (1, 18000)

    # The shopper refreshes, and then asks again from scratch. Neither starts a refund.
    refresh = world.call("POST", "/cases", user="maya", merchant="northline", json_body=body).json()
    assert refresh["replayed"] is True and refresh["case"]["id"] == case["id"]
    retry = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    ).json()
    assert retry["existing_resolution"] is True and retry["case"]["id"] == case["id"]
    assert len(stripe.processor.list_refunds(payment)) == 1

    # The worker restarts. Only committed state survives. Then the retry comes due.
    world.restart()
    world.clock.advance(seconds=world.settings.retry_base_seconds + 1)
    assert world.reconcile().json()["result"] == {"claimed": 1, "succeeded": 1}

    # One refund at Stripe, one successful action, one $180 ledger effect.
    [refund] = stripe.processor.list_refunds(payment)
    [action] = world.actions("northline")
    [entry] = world.ledger("northline")
    assert (refund.amount_cents, refund.status) == (18000, "succeeded")
    assert refund.metadata["action_id"] == str(action["id"])
    assert (action["status"], action["provider_refund_id"]) == ("succeeded", refund.id)
    assert (entry["amount_cents"], entry["provider_refund_id"]) == (18000, refund.id)
    blue = world.line("northline", "1001", "blue")
    assert (blue["reserved_cents"], blue["refunded_cents"]) == (0, 18000)
    green = world.line("northline", "1001", "green")
    assert (green["reserved_cents"], green["refunded_cents"]) == (0, 0)

    # The second attempt found the refund at Stripe instead of sending a new one.
    attempts = world.rows("select attempt_no, operation, outcome from action_attempts order by id")
    assert [(a["attempt_no"], a["operation"], a["outcome"]) for a in attempts] == [
        (1, "create_refund", "unknown"),
        (2, "find_refund", "found"),
    ]

    final = world.call("GET", f"/cases/{case['id']}", user="maya", merchant="northline").json()
    assert final["case"]["state"] == "resolved"
    assert final["case"]["receipt"]["refund_reference"] == refund.id
    assert world.reconcile().json()["result"] == {"claimed": 0}
    assert len(stripe.processor.list_refunds(payment)) == 1


def test_a01_the_plain_refund_with_stripe(stripe: StripeWorld):
    world = stripe.world
    response = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    )

    case = response.json()["case"]
    assert case["state"] == "resolved"
    [refund] = stripe.processor.list_refunds(stripe.payment_ref())
    assert refund.amount_cents == 18000
    assert case["receipt"]["refund_reference"] == refund.id


# --- behaviour pinned against the fake only -----------------------------------------------


@pytest.fixture
def faked(settings: Settings, request: pytest.FixtureRequest) -> Iterator[StripeWorld]:
    fake = FakeStripe()
    lossy = LoseResponses(fake.transport())
    world = World(
        settings.model_copy(update={"payment_provider": "stripe"}),
        "two_jackets",
        request.node.name,
        processor_factory=lambda: StripeProcessor(FakeStripe.KEY, transport=lossy),
    )
    try:
        yield StripeWorld(world, lossy, fake)
    finally:
        world.close()


def refund_request(stripe: StripeWorld) -> dict[str, Any]:
    world = stripe.world
    return world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    ).json()["case"]


def test_the_same_key_replays_the_first_refund_and_never_makes_a_second(faked: StripeWorld):
    processor, payment = faked.processor, faked.payment_ref()
    call = {
        "payment_ref": payment,
        "amount_cents": 18000,
        "idempotency_key": "rd-refund-test",
        "metadata": {"action_id": "a1"},
    }

    first = processor.create_refund(**call)
    again = processor.create_refund(**call)

    assert again.id == first.id
    assert len(processor.list_refunds(payment)) == 1
    assert processor.find_refund(payment_ref=payment, action_id="a1").id == first.id
    assert processor.find_refund(payment_ref=payment, action_id="someone-else") is None
    # The same key with a different amount is our mistake, not a refusal: outcome unknown.
    with pytest.raises(ProcessorTimeout):
        processor.create_refund(**{**call, "amount_cents": 100})


def test_a_refund_stripe_holds_as_pending_is_not_settled_until_it_is_final(faked: StripeWorld):
    world = faked.world
    assert faked.fake is not None
    faked.fake.next_refund_status = "pending"

    case = refund_request(faked)

    # Stripe took the refund but has not finished it. Not paid yet as far as we know.
    assert case["status"] == "pending_verification"
    assert world.ledger("northline") == []
    assert world.line("northline", "1001", "blue")["reserved_cents"] == 18000

    # Stripe finishes it and says so. The signed event settles the action, once.
    [refund] = faked.fake.refunds
    faked.fake.finish(refund["id"])
    payload = webhooks.encode(faked.fake.event(refund["id"]))
    header = webhooks.sign(
        payload, world.settings.payment_webhook_secret, int(world.clock.now().timestamp())
    )
    for expected in ("settled", "settled"):
        response = world.client.post(
            "/webhooks/payments", content=payload, headers={"Stripe-Signature": header}
        )
        assert response.json()["outcome"] == expected
    assert [e["amount_cents"] for e in world.ledger("northline")] == [18000]
    assert world.line("northline", "1001", "blue")["refunded_cents"] == 18000
    # The retry that was queued finds the action final and does nothing.
    world.clock.advance(minutes=30)
    assert world.reconcile().json()["result"] == {"claimed": 0}


def test_a_refund_stripe_refuses_is_a_verified_failure(faked: StripeWorld):
    world, payment = faked.world, faked.payment_ref()
    # The whole payment has already been refunded out of band.
    faked.processor.create_refund(
        payment_ref=payment, amount_cents=30000, idempotency_key="outside", metadata={}
    )

    case = refund_request(faked)

    assert case["state"] == "declined"
    [action] = world.actions("northline")
    assert (action["status"], action["last_error"]) == ("failed", "charge_already_refunded")
    assert world.line("northline", "1001", "blue")["reserved_cents"] == 0
    assert world.ledger("northline") == []


def test_a_stripe_outage_or_a_bad_key_is_not_mistaken_for_a_refusal(faked: StripeWorld):
    world = faked.world
    assert faked.fake is not None
    faked.fake.fail_with = (503, {"type": "api_error", "message": "try again"})

    case = refund_request(faked)

    # Outcome unknown: the unit stays held and nothing is released or declined.
    assert case["state"] == "reconciling"
    assert world.line("northline", "1001", "blue")["reserved_cents"] == 18000

    faked.fake.fail_with = None
    world.clock.advance(seconds=world.settings.retry_base_seconds + 1)
    assert world.reconcile().json()["result"] == {"claimed": 1, "succeeded": 1}
    assert len(faked.fake.refunds) == 1


def test_only_test_mode_keys_are_accepted():
    for key in ("sk_live_abc", "pk_test_abc", ""):
        with pytest.raises(RuntimeError, match="test-mode key"):
            StripeProcessor(key)


def test_stripe_errors_map_to_refused_or_unknown():
    fake = FakeStripe()
    processor = StripeProcessor(FakeStripe.KEY, transport=fake.transport())
    with pytest.raises(ProcessorRejected) as refused:
        processor.create_refund(
            payment_ref="pi_missing", amount_cents=100, idempotency_key="k1", metadata={}
        )
    assert refused.value.code == "resource_missing"

    wrong_key = StripeProcessor("sk_test_someone_elses", transport=fake.transport())
    with pytest.raises(ProcessorTimeout):
        wrong_key.create_refund(
            payment_ref="pi_missing", amount_cents=100, idempotency_key="k2", metadata={}
        )
