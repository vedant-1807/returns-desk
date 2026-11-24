from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from returns_desk.domain.rules import (
    CustomerTerms,
    CustomerTermsRules,
    Decision,
    LineFacts,
    OperationalRules,
    OperationalRulesBody,
    RequestFacts,
    evaluate,
    return_window,
)

LA = "America/Los_Angeles"


def terms(days: int = 30) -> CustomerTerms:
    return CustomerTerms(
        version_id=uuid4(),
        version_label=f"{days}-day returns",
        rules=CustomerTermsRules(
            return_window_days=days,
            immediate_refund_reasons=["damaged"],
            reason_sections={"damaged": "returns.damaged"},
        ),
    )


def operational(limit: int = 20000, threshold: int = 3) -> OperationalRules:
    return OperationalRules(
        version_id=uuid4(),
        version_label="Approval rules v1",
        rules=OperationalRulesBody(
            auto_approve_limit_cents=limit,
            return_history_threshold=threshold,
            return_history_window_days=30,
        ),
    )


def line(unit_paid_cents: int = 18000, quantity: int = 1, reserved: int = 0, refunded: int = 0):
    return LineFacts(
        quantity=quantity,
        unit_paid_cents=unit_paid_cents,
        reserved_quantity=reserved,
        refunded_quantity=refunded,
    )


def request(
    quantity: int = 1,
    reason: str = "damaged",
    delivered_at: datetime | None = datetime(2026, 10, 31, 18, 0, tzinfo=UTC),
    requested_at: datetime = datetime(2026, 11, 20, 18, 0, tzinfo=UTC),
    prior: int = 0,
) -> RequestFacts:
    return RequestFacts(
        quantity=quantity,
        reason=reason,
        requested_at=requested_at,
        delivered_at=delivered_at,
        merchant_timezone=LA,
        prior_completed_returns=prior,
    )


# --- return window ------------------------------------------------------------------------


def test_window_includes_the_last_permitted_day():
    delivered = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)  # Oct 1 in Los Angeles
    last_minute = datetime(2026, 11, 1, 6, 59, tzinfo=UTC)  # Oct 31, 23:59 in Los Angeles
    check = return_window(delivered, last_minute, 30, LA)
    assert check.last_day.isoformat() == "2026-10-31"
    assert check.requested_on.isoformat() == "2026-10-31"
    assert check.within


def test_window_closes_the_day_after():
    delivered = datetime(2026, 10, 1, 20, 0, tzinfo=UTC)
    just_after_midnight = datetime(2026, 11, 1, 7, 1, tzinfo=UTC)  # Nov 1, 00:01 in Los Angeles
    assert not return_window(delivered, just_after_midnight, 30, LA).within


def test_window_uses_the_merchant_calendar_day_not_utc():
    # 03:00 UTC on Oct 2 is still Oct 1 in Los Angeles, so day 0 is Oct 1.
    delivered = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)
    check = return_window(delivered, delivered, 30, LA)
    assert check.delivered_on.isoformat() == "2026-10-01"
    # The same instants in New York fall on Oct 1 as well (23:00 local).
    assert return_window(delivered, delivered, 30, "America/New_York").delivered_on.isoformat() == (
        "2026-10-01"
    )
    # In Tokyo it is already Oct 2.
    assert return_window(delivered, delivered, 30, "Asia/Tokyo").delivered_on.isoformat() == (
        "2026-10-02"
    )


# --- the reference case -------------------------------------------------------------------


def test_reference_case_is_automatic_at_a_200_dollar_limit():
    result = evaluate(line(18000), request(), terms(30), operational(20000))
    assert result.decision is Decision.AUTO
    assert result.amount_cents == 18000
    assert result.cited_sections == ("returns.window", "returns.damaged")


def test_reference_case_goes_to_review_at_a_150_dollar_limit():
    result = evaluate(line(18000), request(), terms(30), operational(15000))
    assert result.decision is Decision.REVIEW
    assert result.review_reasons == ("above_auto_limit",)
    # The limit is internal evidence, never customer evidence.
    assert "auto_approve_limit_cents" not in result.evidence
    assert result.internal_evidence["auto_approve_limit_cents"] == 15000


def test_day_20_is_inside_30_day_terms_and_outside_14_day_terms():
    inside = evaluate(line(), request(), terms(30), operational())
    outside = evaluate(line(), request(), terms(14), operational())
    assert inside.decision is Decision.AUTO
    assert outside.decision is Decision.INELIGIBLE
    assert outside.hard_reasons == ("outside_return_window",)


# --- limits -------------------------------------------------------------------------------


def test_exactly_the_limit_is_automatic_and_one_cent_over_is_not():
    at_limit = evaluate(line(20000), request(), terms(), operational(20000))
    over = evaluate(line(20001), request(), terms(), operational(20000))
    assert at_limit.decision is Decision.AUTO
    assert over.decision is Decision.REVIEW


def test_limit_compares_the_requested_units_not_the_order():
    # Two units at $120 each: one unit is under a $200 limit, two are over it.
    one = evaluate(line(12000, quantity=2), request(quantity=1), terms(), operational(20000))
    two = evaluate(line(12000, quantity=2), request(quantity=2), terms(), operational(20000))
    assert (one.decision, one.amount_cents) == (Decision.AUTO, 12000)
    assert (two.decision, two.amount_cents) == (Decision.REVIEW, 24000)


# --- return history -----------------------------------------------------------------------


def test_two_prior_returns_are_fine_and_three_trigger_review():
    two = evaluate(line(4000), request(prior=2), terms(), operational())
    three = evaluate(line(4000), request(prior=3), terms(), operational())
    assert two.decision is Decision.AUTO
    assert three.decision is Decision.REVIEW
    assert three.review_reasons == ("return_history_threshold",)
    assert three.internal_evidence["prior_completed_returns"] == 3


# --- hard constraints ---------------------------------------------------------------------


def test_an_already_refunded_unit_is_ineligible_whatever_else_is_true():
    result = evaluate(line(18000, refunded=1), request(), terms(), operational())
    assert result.decision is Decision.INELIGIBLE
    assert result.hard_reasons == ("quantity_unavailable",)


def test_reserved_units_count_against_the_remaining_quantity():
    result = evaluate(
        line(5000, quantity=2, reserved=1), request(quantity=2), terms(), operational()
    )
    assert result.decision is Decision.INELIGIBLE


def test_a_line_with_an_earlier_partial_return_still_has_a_refundable_remainder():
    result = evaluate(
        line(5000, quantity=3, refunded=1), request(quantity=2), terms(), operational()
    )
    assert result.decision is Decision.AUTO
    assert result.amount_cents == 10000


def test_hard_constraint_wins_over_review_signals():
    result = evaluate(line(30000, refunded=1), request(prior=5), terms(), operational(20000))
    assert result.decision is Decision.INELIGIBLE


# --- missing evidence ---------------------------------------------------------------------


def test_no_applicable_customer_terms_goes_to_staff():
    result = evaluate(line(), request(), None, operational())
    assert result.decision is Decision.REVIEW
    assert result.review_reasons == ("policy_needs_verification",)
    assert result.cited_sections == ()


def test_no_operational_rules_goes_to_staff():
    result = evaluate(line(), request(), terms(), None)
    assert result.decision is Decision.REVIEW
    assert result.review_reasons == ("policy_needs_verification",)


def test_a_reason_the_policy_does_not_settle_immediately_goes_to_review():
    result = evaluate(line(4000), request(reason="changed_mind"), terms(), operational())
    assert result.decision is Decision.REVIEW
    assert result.review_reasons == ("reason_requires_review",)


def test_unconfirmed_delivery_goes_to_review():
    result = evaluate(line(4000), request(delivered_at=None), terms(), operational())
    assert result.decision is Decision.REVIEW
    assert "delivery_unconfirmed" in result.review_reasons


def test_a_staff_request_for_review_overrides_an_automatic_refund():
    facts = request()
    escalated = RequestFacts(**{**facts.__dict__, "staff_escalated": True})

    assert evaluate(line(), facts, terms(), operational()).decision is Decision.AUTO
    result = evaluate(line(), escalated, terms(), operational())

    assert result.decision is Decision.REVIEW
    assert result.review_reasons == ("staff_escalation",)
    assert result.amount_cents == 18000


def test_a_staff_request_for_review_cannot_rescue_an_ineligible_request():
    late = RequestFacts(
        **{
            **request(requested_at=datetime(2026, 12, 25, 18, 0, tzinfo=UTC)).__dict__,
            "staff_escalated": True,
        }
    )

    result = evaluate(line(), late, terms(), operational())

    assert result.decision is Decision.INELIGIBLE
    assert result.hard_reasons == ("outside_return_window",)
