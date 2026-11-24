"""Role-appropriate views of a case.

Every read here runs under the caller's row-level security, and the shape is built per
role on top of that: a shopper gets customer wording and customer-visible citations, an
associate gets the working detail, and only a supervisor gets the restricted review record.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from returns_desk.db import Conn, Principal
from returns_desk.services import policies

CUSTOMER_STATUS = {
    "open": ("received", "We have your request."),
    "awaiting_customer": ("needs_your_confirmation", "Please confirm the details to continue."),
    "awaiting_supervisor": ("in_review", "Your request is with our team for review."),
    "processing": ("processing", "Your refund is being processed."),
    "reconciling": (
        "pending_verification",
        "Your refund is pending verification with the payment provider.",
    ),
    "resolved": ("refunded", "Your refund has been issued."),
    "declined": ("declined", "This request was not approved."),
}


def customer_status(
    state: str, outcome_type: str | None = None, action: str | None = None
) -> tuple[str, str]:
    """The status a shopper sees, and the sentence that goes with it. The words follow the
    case's real state and what was asked for, never the model."""
    status, message = CUSTOMER_STATUS[state]
    if state == "resolved" and outcome_type == "answered":
        return "answered", "We have answered your question."
    if state == "resolved" and outcome_type == "exchange":
        return "exchanged", "Your exchange is confirmed."
    if action == "exchange" and state == "processing":
        return status, "Your exchange is being processed."
    return status, message


NEXT_ACTION = {
    "awaiting_customer": "confirm",
    "awaiting_supervisor": "wait",
    "processing": "wait",
    "reconciling": "wait",
}

# Outcome reasons that are safe to show a shopper as they are.
CUSTOMER_SAFE_REASONS = {
    "outside_return_window",
    "quantity_unavailable",
    "invalid_quantity",
    "replacement_unavailable",
    # Information-only endings of a chat.
    "policy_question",
    "status",
    "existing_resolution",
    "not_confirmed",
    "no_order",
}


def case_view(conn: Conn, principal: Principal, case_id: UUID) -> dict[str, Any] | None:
    case = conn.execute(
        """
        select c.*, ol.description, ol.variant, ol.sku, o.order_number
        from cases c
        left join order_lines ol on ol.id = c.order_line_id
        left join orders o on o.id = c.order_id
        where c.id = %s
        """,
        (case_id,),
    ).fetchone()
    if case is None:
        return None

    proposal = conn.execute(
        "select * from proposals where case_id = %s order by version desc limit 1", (case_id,)
    ).fetchone()
    action = None
    ledger = None
    if proposal is not None:
        action = conn.execute(
            "select * from resolution_actions where proposal_id = %s", (proposal["id"],)
        ).fetchone()
    if action is not None:
        ledger = conn.execute(
            "select * from ledger_entries where action_id = %s", (action["id"],)
        ).fetchone()

    status, status_message = customer_status(
        case["state"], case["outcome_type"], case["requested_action"]
    )
    exchange = case["requested_action"] == "exchange"
    fulfillment = None
    if action is not None and action["kind"] == "exchange":
        fulfillment = conn.execute(
            "select reference, sku, variant, quantity, created_at from fulfillments "
            "where action_id = %s",
            (action["id"],),
        ).fetchone()
    next_action = NEXT_ACTION.get(case["state"])
    if case["state"] == "awaiting_customer" and not (
        proposal is not None and proposal["status"] == "awaiting_consent"
    ):
        # A clarifying question, not a request to confirm an amount.
        status, status_message = "needs_your_reply", "We need a reply from you to continue."
        next_action = "reply"
    citations: list[dict[str, Any]] = []
    if proposal is not None and proposal["customer_policy_version_id"]:
        # Row-level security returns only the sections this caller's audience may read.
        citations = [
            {
                "version_id": s["version_id"],
                "version_label": s["version_label"],
                "section_key": s["section_key"],
                "title": s["title"],
                "body": s["body"],
            }
            for s in policies.sections_for(
                conn, proposal["customer_policy_version_id"], proposal["cited_sections"]
            )
            if principal.is_staff or s["audience"] == "customer"
        ]

    view: dict[str, Any] = {
        "id": case["id"],
        "state": case["state"],
        "status": status,
        "status_message": status_message,
        "next_action": next_action,
        "requested_action": case["requested_action"],
        "item": {
            "order_id": case["order_id"],
            "order_number": case["order_number"],
            "order_line_id": case["order_line_id"],
            "description": case["description"],
            "variant": case["variant"],
            "quantity": case["quantity"],
        },
        # Money that is or would be refunded. An exchange moves none.
        "amount_cents": proposal["amount_cents"] if proposal and not exchange else None,
        "currency": "USD",
        "exchange": (
            {
                "replacement_variant": case["replacement_variant"],
                # Set once the exchange has been carried out.
                "fulfillment": fulfillment,
            }
            if exchange
            else None
        ),
        "policy_citations": citations,
        "action": _action_view(action),
        "receipt": _receipt_view(action, ledger),
        "outcome": _outcome_view(case, principal),
        # Row-level security leaves internal notes out for a shopper.
        "messages": conn.execute(
            """
            select author_kind, audience, body, created_at from case_messages
            where case_id = %s order by seq
            """,
            (case_id,),
        ).fetchall(),
        "requested_at": case["request_received_at"],
        "updated_at": case["updated_at"],
        "channel": case["channel"],
    }

    if principal.is_staff:
        view["shopper_id"] = case["shopper_id"]
        view["reason_code"] = case["reason_code"]
        view["review_requested"] = (
            {"at": case["review_requested_at"], "by": case["review_requested_by"]}
            if case["review_requested_at"]
            else None
        )
        view["proposal"] = (
            {
                "id": proposal["id"],
                "version": proposal["version"],
                "status": proposal["status"],
                "decision": proposal["decision"],
                "action_kind": proposal["action_kind"],
                # The paid value of the units: what the approval limit is compared with.
                "value_cents": proposal["amount_cents"],
                "evidence": proposal["evidence"],
            }
            if proposal
            else None
        )
        view["decision"] = (
            conn.execute(
                """
                select decision, reviewer_user_id, proposal_version, note, decided_at
                from approvals where proposal_id = %s
                """,
                (proposal["id"],),
            ).fetchone()
            if proposal
            else None
        )
    if principal.role == "supervisor" and proposal is not None:
        # review_records is supervisor-only in the database; this branch just avoids a
        # pointless query for everyone else.
        view["review"] = conn.execute(
            "select reasons, internal_evidence from review_records where proposal_id = %s",
            (proposal["id"],),
        ).fetchone()
    return view


def _action_view(action: dict[str, Any] | None) -> dict[str, Any] | None:
    if action is None:
        return None
    return {"id": action["id"], "kind": action["kind"], "status": action["status"]}


def _receipt_view(
    action: dict[str, Any] | None, ledger: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Only a refund the processor has confirmed gets a receipt."""
    if action is None or ledger is None:
        return None
    return {
        "refund_reference": ledger["provider_refund_id"],
        "amount_cents": ledger["amount_cents"],
        "currency": "USD",
        "confirmed_at": ledger["recorded_at"],
    }


def _outcome_view(case: dict[str, Any], principal: Principal) -> dict[str, Any] | None:
    if case["outcome_type"] is None:
        return None
    reason = case["outcome_reason"]
    if not principal.is_staff and reason not in CUSTOMER_SAFE_REASONS:
        reason = None
    return {"type": case["outcome_type"], "reason": reason}


def list_cases(conn: Conn, principal: Principal, limit: int = 50) -> list[dict[str, Any]]:
    """The caller's cases: a shopper's own, or the merchant's for staff."""
    return conn.execute(
        """
        select c.id, c.state, c.requested_action, c.replacement_variant, c.quantity,
               c.request_received_at, c.updated_at, ol.description, ol.variant, o.order_number,
               -- Flags for the staff queue. A shopper's own list carries them too, but
               -- they say nothing a shopper is not told in the conversation.
               (c.review_requested_at is not null) as review_requested,
               exists (
                 select 1 from case_events e
                 where e.case_id = c.id and e.kind = 'needs_staff'
               ) as needs_staff
        from cases c
        left join order_lines ol on ol.id = c.order_line_id
        left join orders o on o.id = c.order_id
        order by c.updated_at desc, c.id
        limit %s
        """,
        (limit,),
    ).fetchall()


def list_orders(conn: Conn) -> list[dict[str, Any]]:
    """Orders the caller may see, with what is still refundable on each line."""
    orders = conn.execute(
        """
        select id, order_number, purchased_at, delivered_at, currency
        from orders order by purchased_at desc, id
        """
    ).fetchall()
    for order in orders:
        order["lines"] = conn.execute(
            """
            select id, sku, variant, description, quantity, unit_paid_cents,
                   quantity - reserved_quantity - refunded_quantity - exchanged_quantity
                     as remaining_quantity,
                   (quantity - exchanged_quantity)::bigint * unit_paid_cents
                     - reserved_cents - refunded_cents as remaining_cents
            from order_lines where order_id = %s order by description, variant, id
            """,
            (order["id"],),
        ).fetchall()
    return orders
