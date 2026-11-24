from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from returns_desk.db import Conn
from returns_desk.domain.rules import (
    CustomerTerms,
    Evaluation,
    LineFacts,
    OperationalRules,
    RequestFacts,
    evaluate,
)
from returns_desk.services import policies


@dataclass(frozen=True)
class Quote:
    """A computed resolution for one line and quantity. Building a quote changes nothing:
    no reservation, no payment, no stock."""

    evaluation: Evaluation
    terms: CustomerTerms | None
    operational: OperationalRules | None


def load_line_for_update(conn: Conn, order_id: UUID, line_id: UUID) -> dict[str, Any] | None:
    """Read one order line with its order facts and lock the line.

    Row-level security hides lines the caller may not see, so an inaccessible line and a
    missing line both come back as None. The lock serializes everything that reads the
    remaining balance and then acts on it.
    """
    return conn.execute(
        """
        select ol.id, ol.merchant_id, ol.order_id, ol.sku, ol.variant, ol.description,
               ol.quantity, ol.unit_paid_cents, ol.reserved_quantity, ol.reserved_cents,
               ol.refunded_quantity, ol.refunded_cents,
               o.shopper_id, o.order_number, o.purchased_at, o.delivered_at,
               o.provider_payment_ref
        from order_lines ol
        join orders o on o.merchant_id = ol.merchant_id and o.id = ol.order_id
        where ol.id = %s and ol.order_id = %s
        for update of ol
        """,
        (line_id, order_id),
    ).fetchone()


def prior_completed_returns(
    conn: Conn,
    *,
    shopper_id: UUID,
    requested_at: datetime,
    window_days: int,
    exclude_case_id: UUID | None,
) -> int:
    """Distinct completed returns by this shopper at this merchant in the look-back window
    before the request, not counting the current case."""
    row = conn.execute(
        """
        select count(*) as n
        from cases
        where merchant_id = app_merchant()
          and shopper_id = %(shopper)s
          and state = 'resolved'
          and outcome_type = 'refund'
          and resolved_at > %(since)s
          and resolved_at <= %(until)s
          and (%(exclude)s::uuid is null or id <> %(exclude)s::uuid)
        """,
        {
            "shopper": shopper_id,
            "since": requested_at - timedelta(days=window_days),
            "until": requested_at,
            "exclude": exclude_case_id,
        },
    ).fetchone()
    return int(row["n"])


def quote_refund(
    conn: Conn,
    *,
    line: dict[str, Any],
    quantity: int,
    reason: str,
    requested_at: datetime,
    merchant_timezone: str,
    now: datetime,
    exclude_case_id: UUID | None,
    staff_escalated: bool = False,
) -> Quote:
    """Gather the evidence for one refund request and run the rules.

    Customer terms come from the purchase timestamp. Operational rules are the ones active
    right now, when the proposal is prepared. The window is judged at the original request
    time, which the caller passes in.
    """
    terms = policies.customer_terms_at(conn, line["purchased_at"])
    operational = policies.operational_rules_at(conn, now)
    prior = 0
    if operational is not None:
        prior = prior_completed_returns(
            conn,
            shopper_id=line["shopper_id"],
            requested_at=requested_at,
            window_days=operational.rules.return_history_window_days,
            exclude_case_id=exclude_case_id,
        )
    evaluation = evaluate(
        LineFacts(
            quantity=line["quantity"],
            unit_paid_cents=line["unit_paid_cents"],
            reserved_quantity=line["reserved_quantity"],
            refunded_quantity=line["refunded_quantity"],
        ),
        RequestFacts(
            quantity=quantity,
            reason=reason,
            requested_at=requested_at,
            delivered_at=line["delivered_at"],
            merchant_timezone=merchant_timezone,
            prior_completed_returns=prior,
            staff_escalated=staff_escalated,
        ),
        terms,
        operational,
    )
    return Quote(evaluation=evaluation, terms=terms, operational=operational)


def write_proposal(
    conn: Conn,
    *,
    case: dict[str, Any],
    quote: Quote,
    status: str,
    now: datetime,
    consented_at: datetime | None,
) -> dict[str, Any]:
    """Store the next proposal version for a case as an immutable snapshot.

    Any earlier version still waiting on consent or review is marked superseded, so only one
    version can ever be acted on.
    """
    ev = quote.evaluation
    conn.execute(
        """
        update proposals set status = 'superseded'
        where case_id = %s and status in ('awaiting_consent', 'pending_review')
        """,
        (case["id"],),
    )
    version = conn.execute(
        "select coalesce(max(version), 0) + 1 as v from proposals where case_id = %s",
        (case["id"],),
    ).fetchone()["v"]
    proposal = conn.execute(
        """
        insert into proposals
          (merchant_id, case_id, version, action_kind, order_line_id, quantity, amount_cents,
           decision, reason_codes, customer_policy_version_id, cited_sections,
           operational_policy_version_id, evidence, status, consented_at, created_at)
        values (%s, %s, %s, 'refund', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning *
        """,
        (
            case["merchant_id"],
            case["id"],
            version,
            case["order_line_id"],
            case["quantity"],
            ev.amount_cents,
            ev.decision.value,
            list(ev.hard_reasons),
            quote.terms.version_id if quote.terms else None,
            list(ev.cited_sections),
            quote.operational.version_id if quote.operational else None,
            Jsonb(ev.evidence),
            status,
            consented_at,
            now,
        ),
    ).fetchone()
    if ev.review_reasons:
        # Insert only: the caller may be a shopper, who can never read this row back.
        conn.execute(
            """
            insert into review_records
              (proposal_id, merchant_id, reasons, internal_evidence, created_at)
            values (%s, %s, %s, %s, %s)
            """,
            (
                proposal["id"],
                case["merchant_id"],
                list(ev.review_reasons),
                Jsonb(ev.internal_evidence),
                now,
            ),
        )
    return proposal
