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
    action: str = "refund"
    # For an exchange: the wanted variant and how many of it are in stock right now
    # (None when the product has no such variant).
    replacement: dict[str, Any] | None = None


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
               ol.refunded_quantity, ol.refunded_cents, ol.exchanged_quantity,
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


def stock_of(conn: Conn, *, sku: str, variant: str, lock: bool = False) -> dict[str, Any] | None:
    """The stock row for one variant at the caller's merchant. With `lock`, the row is held
    until the transaction ends, so the count read here is the count that will be acted on."""
    return conn.execute(
        "select id, sku, variant, description, available from inventory "
        "where sku = %s and variant = %s" + (" for update" if lock else ""),
        (sku, variant),
    ).fetchone()


def variants_in_stock(conn: Conn, *, sku: str, quantity: int = 1) -> list[str]:
    """Variants of a product that could fill an exchange of `quantity` units right now."""
    rows = conn.execute(
        "select variant from inventory where sku = %s and available >= %s order by variant",
        (sku, quantity),
    ).fetchall()
    return [row["variant"] for row in rows]


def quote_resolution(
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
    action: str = "refund",
    replacement_variant: str | None = None,
    lock_stock: bool = False,
) -> Quote:
    """Gather the evidence for one request, a refund or an exchange, and run the rules.

    Customer terms come from the purchase timestamp. Operational rules are the ones active
    right now, when the proposal is prepared. The window is judged at the original request
    time, which the caller passes in. For an exchange the replacement's stock is read
    fresh; pass `lock_stock` when the result may be acted on in this transaction.
    """
    replacement: dict[str, Any] | None = None
    available: int | None = None
    if action == "exchange":
        stock = (
            stock_of(conn, sku=line["sku"], variant=replacement_variant, lock=lock_stock)
            if replacement_variant
            else None
        )
        available = stock["available"] if stock else None
        replacement = {
            "sku": line["sku"],
            "variant": replacement_variant,
            "description": line["description"],
            "available": available,
        }
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
            exchanged_quantity=line["exchanged_quantity"],
        ),
        RequestFacts(
            quantity=quantity,
            reason=reason,
            requested_at=requested_at,
            delivered_at=line["delivered_at"],
            merchant_timezone=merchant_timezone,
            prior_completed_returns=prior,
            staff_escalated=staff_escalated,
            action=action,
            replacement_available=available,
        ),
        terms,
        operational,
    )
    return Quote(
        evaluation=evaluation,
        terms=terms,
        operational=operational,
        action=action,
        replacement=replacement,
    )


def quote_for_case(
    conn: Conn,
    case: dict[str, Any],
    line: dict[str, Any],
    *,
    merchant_timezone: str,
    now: datetime,
    lock_stock: bool = False,
) -> Quote:
    """The quote for what a case currently asks for. Every path that decides or acts on a
    case goes through here, so a case is always judged on the same facts."""
    return quote_resolution(
        conn,
        line=line,
        quantity=case["quantity"],
        reason=case["reason_code"],
        requested_at=case["request_received_at"],
        merchant_timezone=merchant_timezone,
        now=now,
        exclude_case_id=case["id"],
        staff_escalated=case.get("review_requested_at") is not None,
        action=case["requested_action"] or "refund",
        replacement_variant=case.get("replacement_variant"),
        lock_stock=lock_stock,
    )


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
    version can ever be acted on. `amount_cents` is the paid value of the units: the refund
    amount for a refund, and the value the approval limit was compared with for an exchange.
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
          (merchant_id, case_id, version, action_kind, replacement_sku, replacement_variant,
           order_line_id, quantity, amount_cents,
           decision, reason_codes, customer_policy_version_id, cited_sections,
           operational_policy_version_id, evidence, status, consented_at, created_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        returning *
        """,
        (
            case["merchant_id"],
            case["id"],
            version,
            quote.action,
            quote.replacement["sku"] if quote.replacement else None,
            quote.replacement["variant"] if quote.replacement else None,
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
