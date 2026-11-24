"""Case flow around the acceptance tests: consent, duplicates under concurrency, and the
places a request can be stopped before any money moves."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from returns_desk.seeds import Ids
from tests.conftest import World

NORTHLINE_PAYMENT = Ids.payment_ref("northline", "1001")


def test_without_consent_the_case_waits_for_the_shopper(world: World):
    # "Can I return this?" asks for information. It is not consent to a refund.
    asked = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline", consent=False),
    ).json()["case"]

    assert asked["state"] == "awaiting_customer"
    assert asked["status"] == "needs_your_confirmation"
    assert asked["next_action"] == "confirm"
    assert asked["amount_cents"] == 18000
    assert "$180.00" in asked["messages"][-1]["body"]
    assert world.processor.list_requests(NORTHLINE_PAYMENT) == []
    assert world.actions("northline") == []

    # A message without consent changes nothing.
    still = world.call(
        "POST",
        f"/cases/{asked['id']}/messages",
        user="maya",
        merchant="northline",
        json_body={"client_request_id": "m-1", "text": "How long does it take?"},
    ).json()["case"]
    assert still["state"] == "awaiting_customer"
    assert world.actions("northline") == []

    # Explicit consent resumes the same case and executes once.
    confirm = {"client_request_id": "m-2", "text": "Yes, please refund it.", "consent": True}
    done = world.call(
        "POST",
        f"/cases/{asked['id']}/messages",
        user="maya",
        merchant="northline",
        json_body=confirm,
    ).json()["case"]
    assert done["id"] == asked["id"]
    assert done["state"] == "resolved"
    assert done["receipt"]["amount_cents"] == 18000

    # Sending the same confirmation again is a no-op.
    replay = world.call(
        "POST",
        f"/cases/{asked['id']}/messages",
        user="maya",
        merchant="northline",
        json_body=confirm,
    ).json()
    assert replay["replayed"] is True
    assert len(world.processor.list_refunds(NORTHLINE_PAYMENT)) == 1
    assert len(world.actions("northline")) == 1

    # The window was judged at the original request time, not at confirmation.
    proposals = world.rows(
        "select version, status, consented_at from proposals where case_id = %s order by version",
        (asked["id"],),
    )
    assert [(p["version"], p["status"]) for p in proposals] == [(1, "superseded"), (2, "submitted")]
    assert proposals[0]["consented_at"] is None and proposals[1]["consented_at"] is not None


def test_the_window_is_judged_at_the_original_request_time(world: World):
    # Asked on day 20 of a 30-day window, confirmed two weeks later on day 34.
    asked = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline", consent=False),
    ).json()["case"]
    world.clock.advance(timedelta(days=14))

    done = world.call(
        "POST",
        f"/cases/{asked['id']}/messages",
        user="maya",
        merchant="northline",
        json_body={"client_request_id": "late-yes", "consent": True},
    ).json()["case"]

    assert done["state"] == "resolved"
    window = world.rows(
        "select evidence from proposals where case_id = %s order by version desc limit 1",
        (asked["id"],),
    )[0]["evidence"]["window"]
    assert window["requested_on"] == "2026-11-20"


def test_a_request_outside_the_window_is_declined_with_the_balance_intact(world: World):
    # Day 31 after delivery under the 30-day terms.
    world.clock.advance(timedelta(days=11))

    case = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    ).json()["case"]

    assert case["state"] == "declined"
    assert case["outcome"] == {"type": "declined_ineligible", "reason": "outside_return_window"}
    assert {c["version_label"] for c in case["policy_citations"]} == {"30-day returns"}
    assert world.processor.list_requests(NORTHLINE_PAYMENT) == []
    blue = world.line("northline", "1001", "blue")
    assert (blue["reserved_cents"], blue["refunded_cents"]) == (0, 0)


def test_the_last_day_of_the_window_is_still_inside(world: World):
    # Day 30 exactly.
    world.clock.advance(timedelta(days=10))
    case = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    ).json()["case"]
    assert case["state"] == "resolved"


def test_two_tabs_submitting_at_once_produce_one_refund(world: World):
    # Different client request IDs, same jacket, at the same moment.
    bodies = [world.refund_request("northline") for _ in range(6)]
    headers = world.auth("maya", "northline")

    def submit(body: dict) -> dict:
        response = world.client.post("/cases", headers=headers, json=body)
        assert response.status_code == 200
        return response.json()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(submit, bodies))

    assert len({r["case"]["id"] for r in results}) == 1
    assert sum(1 for r in results if not r["existing_resolution"]) == 1
    assert len(world.rows("select 1 from cases")) == 1
    assert [(a["status"], a["amount_cents"]) for a in world.actions("northline")] == [
        ("succeeded", 18000)
    ]
    assert [r.amount_cents for r in world.processor.list_refunds(NORTHLINE_PAYMENT)] == [18000]
    assert len(world.ledger("northline")) == 1


def test_the_same_client_request_sent_twice_at_once_is_one_case(world: World):
    body = world.refund_request("northline")
    headers = world.auth("maya", "northline")

    def submit(_: int) -> dict:
        response = world.client.post("/cases", headers=headers, json=body)
        assert response.status_code == 200
        return response.json()

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, range(4)))

    assert len({r["case"]["id"] for r in results}) == 1
    assert len(world.rows("select 1 from cases")) == 1
    assert len(world.processor.list_refunds(NORTHLINE_PAYMENT)) == 1


def test_asking_for_more_units_than_were_bought_is_declined(world: World):
    case = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline", quantity=2),
    ).json()["case"]
    assert case["state"] == "declined"
    assert case["outcome"]["reason"] == "quantity_unavailable"
    assert world.processor.list_requests(NORTHLINE_PAYMENT) == []


def test_staff_cannot_open_a_request_and_bad_input_is_rejected(world: World):
    body = world.refund_request("northline")
    assert (
        world.call("POST", "/cases", user="sam", merchant="northline", json_body=body).status_code
        == 403
    )
    assert world.call("POST", "/cases", json_body=body).status_code == 401

    # The model, or anyone else, cannot smuggle an amount or a merchant into the request.
    for extra in ({"amount_cents": 50000}, {"merchant_id": str(Ids.merchant("vale"))}):
        response = world.call(
            "POST", "/cases", user="maya", merchant="northline", json_body={**body, **extra}
        )
        assert response.status_code == 422
    assert world.rows("select 1 from cases") == []


def test_reconcile_needs_the_internal_secret(world: World):
    assert world.call("POST", "/internal/reconcile").status_code == 401
    as_user = world.call("POST", "/internal/reconcile", user="sam", merchant="northline")
    assert as_user.status_code == 401
    assert world.reconcile().status_code == 200
