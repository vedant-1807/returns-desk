"""A05 (policy versions), A07 (ownership and tenant isolation) and A10 (return history)."""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest

from returns_desk.seeds import Ids
from tests.conftest import World

pytestmark = pytest.mark.acceptance

NORTHLINE_PAYMENT = Ids.payment_ref("northline", "1001")
UNKNOWN_ID = "00000000-0000-4000-8000-000000000000"


# --- A05 ----------------------------------------------------------------------------------


def test_a05_the_purchase_date_policy_is_cited_not_the_current_one(world: World):
    # Bought under 30-day terms. A 14-day version took effect 10 days ago and is current.
    # The request arrives 20 days after delivery.
    versions = world.rows(
        "select version_label, effective_to from policy_versions where policy_id = %s "
        "order by effective_from",
        (Ids.policy("northline", "customer_terms"),),
    )
    assert [v["version_label"] for v in versions] == ["30-day returns", "14-day returns"]
    assert versions[0]["effective_to"] is not None and versions[1]["effective_to"] is None

    case = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    ).json()["case"]

    # Decided and cited under the version in force at purchase.
    assert case["state"] == "resolved"
    assert {c["version_label"] for c in case["policy_citations"]} == {"30-day returns"}
    assert "within 30 days" in case["policy_citations"][0]["body"]

    proposal = world.rows("select * from proposals where case_id = %s", (case["id"],))[0]
    assert proposal["customer_policy_version_id"] == Ids.policy_version(
        "northline", "customer_terms", "30-day"
    )
    # The window ran from delivery to the original request time, in the merchant's calendar.
    window = proposal["evidence"]["window"]
    assert window["window_days"] == 30
    assert window["delivered_on"] == "2026-10-31"
    assert window["requested_on"] == "2026-11-20"
    assert window["last_day"] == "2026-11-30"
    assert window["within"] is True
    # Under the current 14-day terms the same request would have been 6 days late.
    assert window["requested_on"] > "2026-11-14"


def test_a05_a_new_approval_limit_is_assessed_independently(world: World):
    # A supervisor lowers Northline's automatic limit from $200 to $150, effective now.
    published = world.call(
        "POST",
        f"/policies/{Ids.policy('northline', 'operational')}/versions",
        user="sam",
        merchant="northline",
        json_body={
            "version_label": "Approval rules v2",
            "effective_from": world.clock.now().isoformat(),
            "rules": {
                "auto_approve_limit_cents": 15000,
                "return_history_threshold": 3,
                "return_history_window_days": 30,
            },
            "sections": [
                {
                    "section_key": "ops.approval_limit",
                    "title": "Approval limit",
                    "audience": "supervisor",
                    "body": "Refunds above $150.00 require supervisor review.",
                }
            ],
        },
    )
    assert published.status_code == 201
    new_version_id = published.json()["version"]["id"]

    case = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    ).json()["case"]

    # The operational change sends it to review ...
    assert case["state"] == "awaiting_supervisor"
    assert world.processor.list_requests(NORTHLINE_PAYMENT) == []
    # ... while the customer terms are still the ones from the purchase date.
    assert {c["version_label"] for c in case["policy_citations"]} == {"30-day returns"}

    # Both versions are recorded on the proposal, each chosen by its own rule.
    proposal = world.rows("select * from proposals where case_id = %s", (case["id"],))[0]
    assert proposal["customer_policy_version_id"] == Ids.policy_version(
        "northline", "customer_terms", "30-day"
    )
    assert str(proposal["operational_policy_version_id"]) == new_version_id

    review = world.call("GET", "/reviews", user="sam", merchant="northline").json()["reviews"][0]
    assert review["reasons"] == ["above_auto_limit"]
    assert review["internal_rule"]["version_label"] == "Approval rules v2"

    # The old operational version was closed, not edited, and nothing overlaps.
    versions = world.rows(
        "select version_label, effective_to, rules from policy_versions where policy_id = %s "
        "order by effective_from",
        (Ids.policy("northline", "operational"),),
    )
    assert [v["version_label"] for v in versions] == ["Approval rules v1", "Approval rules v2"]
    assert versions[0]["effective_to"] == world.clock.now()
    assert versions[0]["rules"]["auto_approve_limit_cents"] == 20000

    # Vale's rules are untouched by Northline's change.
    vale = world.rows(
        "select count(*) as n from policy_versions where policy_id = %s",
        (Ids.policy("vale", "operational"),),
    )
    assert vale[0]["n"] == 1


def test_a05_a_rule_change_between_proposal_and_approval_needs_a_fresh_decision(world: World):
    """Operational rules are rechecked when the action is authorized. If they changed in a
    way that affects authorization, the supervisor must decide on a new proposal version."""
    vale_payment = Ids.payment_ref("vale", "1001")
    # Maya returns the green jacket at Vale first ($120, automatic).
    world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="vale",
        json_body=world.refund_request("vale", line="green"),
    )
    world.clock.advance(minutes=5)

    # Then the blue one: $180 is above Vale's $150 line, so it waits for review.
    blue = world.call(
        "POST", "/cases", user="maya", merchant="vale", json_body=world.refund_request("vale")
    ).json()["case"]
    first = world.call("GET", "/reviews", user="vera", merchant="vale").json()["reviews"][0]
    assert first["reasons"] == ["above_auto_limit"]
    assert first["proposal_version"] == 1

    # While it waits, Vale tightens its history rule: one earlier return now needs review.
    world.clock.advance(minutes=5)
    published = world.call(
        "POST",
        f"/policies/{Ids.policy('vale', 'operational')}/versions",
        user="vera",
        merchant="vale",
        json_body={
            "version_label": "Approval rules v2",
            "effective_from": world.clock.now().isoformat(),
            "rules": {
                "auto_approve_limit_cents": 15000,
                "return_history_threshold": 1,
                "return_history_window_days": 30,
            },
        },
    )
    assert published.status_code == 201
    v2 = published.json()["version"]["id"]

    # Approving the version she was shown is refused: it no longer matches the rules.
    stale = world.call(
        "POST",
        f"/reviews/{first['review_id']}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "approve"},
    )
    assert stale.status_code == 409
    current = stale.json()["error"]["details"]["current"]
    assert current["proposal_version"] == 2
    assert current["review_id"] != first["review_id"]
    # No approval was recorded and no blue-jacket refund was sent.
    assert world.rows("select 1 from approvals") == []
    assert [r.amount_cents for r in world.processor.list_refunds(vale_payment)] == [12000]

    # The queue now shows the new version with both reasons.
    second = world.call("GET", "/reviews", user="vera", merchant="vale").json()["reviews"]
    assert [r["review_id"] for r in second] == [current["review_id"]]
    assert second[0]["reasons"] == ["above_auto_limit", "return_history_threshold"]
    assert second[0]["internal_rule"]["version_label"] == "Approval rules v2"

    # Asking about the old version again points at the current one.
    again = world.call(
        "POST",
        f"/reviews/{first['review_id']}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "approve"},
    )
    assert again.status_code == 409
    assert again.json()["error"]["details"]["current"]["review_id"] == current["review_id"]

    # Deciding the current version works, once.
    approved = world.call(
        "POST",
        f"/reviews/{current['review_id']}/decision",
        user="vera",
        merchant="vale",
        json_body={"decision": "approve", "note": "Reviewed under v2."},
    )
    assert approved.status_code == 200
    assert approved.json()["case"]["id"] == blue["id"]
    assert approved.json()["case"]["state"] == "resolved"
    assert sorted(r.amount_cents for r in world.processor.list_refunds(vale_payment)) == [
        12000,
        18000,
    ]

    # Both operational versions are on record: the one each proposal was prepared under,
    # and the one rechecked when the action was authorized.
    proposals = world.rows(
        "select version, status, operational_policy_version_id from proposals "
        "where case_id = %s order by version",
        (blue["id"],),
    )
    assert [(p["version"], p["status"]) for p in proposals] == [(1, "superseded"), (2, "approved")]
    assert proposals[0]["operational_policy_version_id"] == Ids.policy_version(
        "vale", "operational", "v1"
    )
    assert str(proposals[1]["operational_policy_version_id"]) == v2
    action = world.rows(
        "select operational_policy_version_id from resolution_actions where case_id = %s",
        (blue["id"],),
    )[0]
    assert str(action["operational_policy_version_id"]) == v2


def test_a05_publishing_rules(world: World):
    path = f"/policies/{Ids.policy('northline', 'customer_terms')}/versions"
    body = {
        "version_label": "7-day returns",
        "effective_from": (world.clock.now() + timedelta(days=3)).isoformat(),
        "rules": {"return_window_days": 7, "immediate_refund_reasons": ["damaged"]},
    }

    # Only a supervisor of this merchant may publish.
    assert (
        world.call("POST", path, user="nina", merchant="northline", json_body=body).status_code
        == 403
    )
    assert (
        world.call("POST", path, user="maya", merchant="northline", json_body=body).status_code
        == 403
    )
    assert world.call("POST", path, user="vera", merchant="vale", json_body=body).status_code == 404

    # Not in the past, and the rules must fit the policy type.
    past = {**body, "effective_from": (world.clock.now() - timedelta(days=1)).isoformat()}
    assert (
        world.call("POST", path, user="sam", merchant="northline", json_body=past).status_code
        == 422
    )
    wrong = {**body, "rules": {"auto_approve_limit_cents": 1}}
    assert (
        world.call("POST", path, user="sam", merchant="northline", json_body=wrong).status_code
        == 422
    )

    # A future-effective revision is accepted and leaves today's decisions alone.
    assert (
        world.call("POST", path, user="sam", merchant="northline", json_body=body).status_code
        == 201
    )
    case = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline"),
    ).json()["case"]
    assert case["state"] == "resolved"
    assert {c["version_label"] for c in case["policy_citations"]} == {"30-day returns"}

    # The read-only list shows a shopper customer terms only, never operational rules.
    listed = world.call("GET", "/policies", user="maya", merchant="northline").json()["policies"]
    assert [p["policy_type"] for p in listed] == ["customer_terms"]
    assert [v["version_label"] for v in listed[0]["versions"]] == [
        "30-day returns",
        "14-day returns",
        "7-day returns",
    ]
    supervisor = world.call("GET", "/policies", user="sam", merchant="northline").json()["policies"]
    assert sorted(p["policy_type"] for p in supervisor) == ["customer_terms", "operational"]


# --- A07 ----------------------------------------------------------------------------------


def test_a07_another_shoppers_order_looks_like_an_unknown_order(world: World):
    # Maya asks for a refund on Omar's Northline order.
    theirs = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline", number="2001", line="tote"),
    )
    # The same request for an order that does not exist anywhere.
    nothing = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body={
            **world.refund_request("northline"),
            "order_id": str(uuid4()),
            "order_line_id": str(uuid4()),
        },
    )
    # Her own order at another merchant, sent with her Northline sign-in.
    other_tenant = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("vale"),
    )

    for response in (theirs, nothing, other_tenant):
        assert response.status_code == 404
        assert response.json() == {"error": {"code": "not_found", "message": "Not found"}}

    # No disclosed details, no provider call, no mutation.
    assert world.rows("select 1 from cases") == []
    assert world.rows("select 1 from proposals") == []
    assert world.rows("select 1 from resolution_actions") == []
    assert world.processor.list_requests() == []
    assert (
        world.rows(
            "select 1 from order_lines where reserved_quantity <> 0 or refunded_quantity <> 0"
        )
        == []
    )

    # Listing orders shows only her own.
    orders = world.call("GET", "/orders", user="maya", merchant="northline").json()["orders"]
    assert [o["order_number"] for o in orders] == ["NL-1001"]


def test_a07_another_tenants_case_looks_like_an_unknown_case(world: World):
    # Maya has a real case at Vale, waiting for review.
    vale_case = world.call(
        "POST", "/cases", user="maya", merchant="vale", json_body=world.refund_request("vale")
    ).json()["case"]
    before = world.app_side_snapshot()

    callers = [
        ("omar", "vale"),  # another shopper, same merchant
        ("maya", "northline"),  # the owner, signed in to a different merchant
        ("omar", "northline"),  # another shopper, another merchant
        ("nina", "northline"),  # staff of another merchant
        ("sam", "northline"),  # a supervisor of another merchant
    ]
    message = {"client_request_id": "probe-1", "text": "hello", "consent": True}
    for user, merchant in callers:
        read = world.call("GET", f"/cases/{vale_case['id']}", user=user, merchant=merchant)
        unknown = world.call("GET", f"/cases/{UNKNOWN_ID}", user=user, merchant=merchant)
        assert read.status_code == unknown.status_code == 404, (user, merchant)
        assert read.json() == unknown.json()

        write = world.call(
            "POST",
            f"/cases/{vale_case['id']}/messages",
            user=user,
            merchant=merchant,
            json_body=message,
        )
        assert write.status_code == 404, (user, merchant)

        listed = world.call("GET", "/cases", user=user, merchant=merchant).json()["cases"]
        assert vale_case["id"] not in [c["id"] for c in listed]

    # Unauthenticated callers get nothing either.
    assert world.call("GET", f"/cases/{vale_case['id']}").status_code == 401

    # Nothing changed and nothing reached the processor.
    assert world.app_side_snapshot() == before
    assert world.rows("select count(*) as n from case_messages where body = 'hello'")[0]["n"] == 0
    assert world.processor.list_requests() == []

    # The owner, signed in to the right merchant, and that merchant's staff still see it.
    assert (
        world.call("GET", f"/cases/{vale_case['id']}", user="maya", merchant="vale").status_code
        == 200
    )
    assert (
        world.call("GET", f"/cases/{vale_case['id']}", user="alex", merchant="vale").status_code
        == 200
    )


# --- A10 ----------------------------------------------------------------------------------


@pytest.mark.seed("return_history")
def test_a10_three_recent_returns_send_a_small_refund_to_review(world: World):
    beanie_payment = Ids.payment_ref("northline", "1200")

    response = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline", number="1200", line="beanie"),
    )
    case = response.json()["case"]

    # $40 is far below the $200 limit and inside the window, yet it goes to review.
    assert case["amount_cents"] == 4000
    assert case["state"] == "awaiting_supervisor"
    assert case["status"] == "in_review"

    # No provider call, no reservation, no ledger entry.
    assert world.processor.list_requests(beanie_payment) == []
    assert world.actions("northline", "1200") == []
    assert world.ledger("northline", "1200") == []

    # The shopper sees neutral wording: nothing about history or why.
    shopper_text = str(case).lower()
    assert "review" not in case and "proposal" not in case
    for word in ("history", "threshold", "prior", "return_history"):
        assert word not in shopper_text

    # The associate sees the case is in review, but not the history behind it.
    associate = world.call("GET", f"/cases/{case['id']}", user="nina", merchant="northline").json()[
        "case"
    ]
    assert associate["state"] == "awaiting_supervisor"
    assert "review" not in associate
    assert "history" not in str(associate).lower()
    assert world.call("GET", "/reviews", user="nina", merchant="northline").status_code == 403

    # Only the supervisor sees the reason and the count.
    review = world.call("GET", "/reviews", user="sam", merchant="northline").json()["reviews"][0]
    assert review["case_id"] == case["id"]
    assert review["reasons"] == ["return_history_threshold"]
    assert review["internal_evidence"]["prior_completed_returns"] == 3
    assert review["internal_evidence"]["return_history_threshold"] == 3
    supervisor = world.call("GET", f"/cases/{case['id']}", user="sam", merchant="northline").json()[
        "case"
    ]
    assert supervisor["review"]["reasons"] == ["return_history_threshold"]

    # The audit trail, which associates can read, does not carry the reason either.
    events = world.rows("select detail from case_events where case_id = %s", (case["id"],))
    assert "history" not in str(events).lower()


@pytest.mark.seed("return_history")
def test_a10_returns_older_than_30_days_do_not_count(world: World):
    # Push the oldest of the three completed returns out of the 30-day look-back.
    world.rows(
        "update cases set resolved_at = %s where client_request_id = 'seed-history-1' returning id",
        (world.clock.now() - timedelta(days=31),),
    )

    case = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="northline",
        json_body=world.refund_request("northline", number="1200", line="beanie"),
    ).json()["case"]

    # Two in the window is under the threshold: refunded automatically.
    assert case["state"] == "resolved"
    assert case["receipt"]["amount_cents"] == 4000


@pytest.mark.seed("return_history")
def test_a10_history_is_counted_per_merchant(world: World):
    # The same shopper's three Northline returns say nothing about her at Vale, where only
    # the $150 line matters. The $120 green jacket is refunded automatically there.
    case = world.call(
        "POST",
        "/cases",
        user="maya",
        merchant="vale",
        json_body=world.refund_request("vale", line="green"),
    ).json()["case"]
    assert case["state"] == "resolved"
    assert case["amount_cents"] == 12000
