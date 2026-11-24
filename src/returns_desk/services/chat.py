"""Building blocks for conversations run by the agent.

A chat case can open with nothing but the shopper's words. These functions let the
workflow fill it in step by step: record messages, quote a resolution without changing
any balance, submit a quoted proposal, ask a question, or give an answer.

Every function takes an open transaction that already carries the caller's claims, so
row-level security applies to everything here exactly as it does on the typed path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from returns_desk.db import Conn, Principal
from returns_desk.domain.rules import Decision
from returns_desk.errors import Conflict, Forbidden, InvalidRequest, NotFound
from returns_desk.services import policies
from returns_desk.services.cases import _existing_case_for_units, _propose_and_route
from returns_desk.services.common import add_message, move_case, record_event
from returns_desk.services.quotes import load_line_for_update, quote_refund, write_proposal

# States from which a shopper's new message continues the same request.
REPLYABLE_STATES = ("open", "awaiting_customer")


class ChatOpenInput(BaseModel):
    """Start a conversation: just the shopper's words, optionally about a chosen order."""

    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4000)
    order_id: UUID | None = None


def open_chat_case(
    conn: Conn,
    principal: Principal,
    data: ChatOpenInput,
    *,
    now: datetime,
    request_id: str | None,
) -> tuple[dict[str, Any], bool]:
    """Create the case and store the first message. Returns (case, replayed)."""
    if principal.role != "shopper" or principal.shopper_id is None:
        raise Forbidden("Only a signed-in shopper can open a request")

    if data.order_id is not None:
        # Row-level security hides other shoppers' orders, so a foreign order ID and a
        # made-up one fail the same way.
        owned = conn.execute(
            "select 1 from orders where id = %s and shopper_id = %s",
            (data.order_id, principal.shopper_id),
        ).fetchone()
        if owned is None:
            raise NotFound()

    case = conn.execute(
        """
        insert into cases
          (merchant_id, shopper_id, client_request_id, order_id, state, channel,
           request_received_at, created_at, updated_at)
        values (%s, %s, %s, %s, 'open', 'chat', %s, %s, %s)
        on conflict (merchant_id, shopper_id, client_request_id) do nothing
        returning *
        """,
        (
            principal.merchant_id,
            principal.shopper_id,
            data.client_request_id,
            data.order_id,
            now,
            now,
            now,
        ),
    ).fetchone()
    if case is None:
        # A refresh or a double submit: the same client request already has its case.
        existing = conn.execute(
            "select * from cases where shopper_id = %s and client_request_id = %s",
            (principal.shopper_id, data.client_request_id),
        ).fetchone()
        return existing, True

    record_event(
        conn,
        merchant_id=principal.merchant_id,
        case_id=case["id"],
        kind="case_opened",
        to_state="open",
        actor_kind="shopper",
        actor_user_id=principal.user_id,
        request_id=request_id,
        detail={
            "client_request_id": data.client_request_id,
            "channel": "chat",
            # Whether the shopper picked an order first. It decides which lines the agent
            # is shown, so a replay of this conversation needs to know.
            "order_id": str(data.order_id) if data.order_id else None,
        },
        now=now,
    )
    add_message(
        conn,
        merchant_id=principal.merchant_id,
        case_id=case["id"],
        author_kind="shopper",
        author_user_id=principal.user_id,
        body=data.message,
        client_request_id=data.client_request_id,
        now=now,
    )
    return case, False


def load_case(conn: Conn, case_id: UUID, *, lock: bool = False) -> dict[str, Any]:
    """The case, if the caller may see it. Unknown and forbidden look the same."""
    case = conn.execute(
        "select * from cases where id = %s" + (" for update" if lock else ""), (case_id,)
    ).fetchone()
    if case is None:
        raise NotFound()
    return case


def record_reply(
    conn: Conn,
    principal: Principal,
    case: dict[str, Any],
    *,
    text: str,
    client_request_id: str,
    now: datetime,
    request_id: str | None,
) -> bool:
    """Store a shopper's follow-up message. Returns False when this client request was
    already recorded, so the caller can skip running the turn twice."""
    seen = conn.execute(
        "select 1 from case_messages where case_id = %s and client_request_id = %s",
        (case["id"], client_request_id),
    ).fetchone()
    if seen is not None:
        return False
    add_message(
        conn,
        merchant_id=case["merchant_id"],
        case_id=case["id"],
        author_kind="shopper",
        author_user_id=principal.user_id,
        body=text,
        client_request_id=client_request_id,
        now=now,
    )
    answered = case["state"] == "resolved" and case["outcome_type"] == "answered"
    if case["state"] == "awaiting_customer" or answered:
        # The shopper answered a question, or came back after an information-only answer.
        conn.execute(
            """
            update cases
            set state = 'open', outcome_type = null, outcome_reason = null,
                resolved_at = null, updated_at = %s
            where id = %s
            """,
            (now, case["id"]),
        )
        record_event(
            conn,
            merchant_id=case["merchant_id"],
            case_id=case["id"],
            kind="shopper_replied",
            from_state=case["state"],
            to_state="open",
            actor_kind="shopper",
            actor_user_id=principal.user_id,
            request_id=request_id,
            now=now,
        )
        case["state"] = "open"
        case["outcome_type"] = None
    return True


def orders_for_case(
    conn: Conn, case: dict[str, Any], *, order_number: str | None = None, limit: int = 5
) -> list[dict[str, Any]]:
    """The order a case is about, or the case's shopper's recent orders when none is
    chosen yet. Each order comes with its lines, what is still refundable, and any
    existing actions."""
    if case["order_id"] is not None:
        where, params = "o.id = %s", [case["order_id"]]
    elif order_number:
        where, params = (
            "o.shopper_id = %s and o.order_number = %s",
            [
                case["shopper_id"],
                order_number,
            ],
        )
    else:
        where, params = "o.shopper_id = %s", [case["shopper_id"]]
    orders = conn.execute(
        f"""
        select o.id, o.order_number, o.purchased_at, o.delivered_at, o.currency
        from orders o
        where {where}
        order by o.purchased_at desc, o.id
        limit %s
        """,
        [*params, limit],
    ).fetchall()
    for order in orders:
        order["lines"] = conn.execute(
            """
            select id, sku, variant, description, quantity, unit_paid_cents,
                   quantity - reserved_quantity - refunded_quantity as remaining_quantity,
                   quantity::bigint * unit_paid_cents - reserved_cents - refunded_cents
                     as remaining_cents
            from order_lines where order_id = %s
            order by description, variant, id
            """,
            (order["id"],),
        ).fetchall()
        order["actions"] = conn.execute(
            """
            select a.id, a.case_id, a.order_line_id, a.kind, a.status::text as status,
                   a.quantity, a.amount_cents, a.completed_at
            from resolution_actions a
            where a.order_id = %s
            order by a.created_at, a.id
            """,
            (order["id"],),
        ).fetchall()
        # Requests still in progress that have no action yet (waiting for confirmation or
        # for a supervisor), so a status question can find them too.
        order["open_cases"] = conn.execute(
            """
            select c.id, c.order_line_id, c.state::text as state, c.quantity
            from cases c
            where c.order_id = %s and c.order_line_id is not null
              and c.state in ('awaiting_supervisor', 'processing', 'reconciling')
            order by c.created_at, c.id
            """,
            (order["id"],),
        ).fetchall()
    return orders


def quote(
    conn: Conn,
    principal: Principal,
    case: dict[str, Any],
    *,
    order_line_id: UUID,
    quantity: int,
    reason: str,
    now: datetime,
) -> dict[str, Any]:
    """Compute a resolution for one line and quantity and store it as a proposal.

    Changes no financial state: nothing is reserved, nothing is sent to the processor and
    the case does not change state. Amounts and eligibility are computed here on the
    server from the order and the policies, never taken from the caller.
    """
    if case["state"] not in REPLYABLE_STATES:
        raise Conflict("This case can no longer be quoted", details={"state": case["state"]})
    if quantity < 1:
        raise InvalidRequest("Quantity must be at least 1")

    located = conn.execute(
        "select order_id from order_lines where id = %s", (order_line_id,)
    ).fetchone()
    if located is None:
        raise NotFound()
    line = load_line_for_update(conn, located["order_id"], order_line_id)
    if line is None or line["shopper_id"] != case["shopper_id"]:
        raise NotFound()

    # Point the case at this line. The workflow may quote again if the shopper changes
    # their mind; each quote is a new proposal version.
    conn.execute(
        """
        update cases
        set order_id = %s, order_line_id = %s, quantity = %s, requested_action = 'refund',
            reason_code = %s, updated_at = %s
        where id = %s
        """,
        (line["order_id"], line["id"], quantity, reason, now, case["id"]),
    )
    case.update(
        order_id=line["order_id"],
        order_line_id=line["id"],
        quantity=quantity,
        requested_action="refund",
        reason_code=reason,
    )

    # Units already refunded, or claimed by another request still in progress, belong to
    # that request. Surface it instead of quoting the same units twice.
    existing = _existing_case_for_units(conn, line, quantity, exclude_case_id=case["id"])

    result = quote_refund(
        conn,
        line=line,
        quantity=quantity,
        reason=reason,
        requested_at=case["request_received_at"],
        merchant_timezone=principal.merchant_timezone,
        now=now,
        exclude_case_id=case["id"],
        staff_escalated=case.get("review_requested_at") is not None,
    )
    ev = result.evaluation
    eligible = ev.decision is not Decision.INELIGIBLE and existing is None
    proposal = write_proposal(
        conn,
        case=case,
        quote=result,
        status="awaiting_consent" if ev.decision is not Decision.INELIGIBLE else "ineligible",
        now=now,
        consented_at=None,
    )
    citations = []
    if result.terms is not None:
        citations = [
            {
                "version_label": s["version_label"],
                "section_key": s["section_key"],
                "title": s["title"],
                "body": s["body"],
            }
            for s in policies.sections_for(conn, result.terms.version_id, list(ev.cited_sections))
        ]
    reason_codes = list(ev.hard_reasons)
    if existing is not None and "quantity_unavailable" not in reason_codes:
        reason_codes.append("quantity_unavailable")
    return {
        "proposal_id": proposal["id"],
        "proposal_version": proposal["version"],
        "eligible": eligible,
        "amount_cents": ev.amount_cents,
        "currency": "USD",
        # True when a person must approve before any payment. The reasons stay internal.
        "requires_review": ev.decision is Decision.REVIEW,
        "reason_codes": reason_codes,
        "evidence": ev.evidence,
        "citations": citations,
        "existing_case_id": existing,
        "item": {
            "order_number": line["order_number"],
            "description": line["description"],
            "variant": line["variant"],
            "quantity": quantity,
        },
    }


def submit(
    conn: Conn,
    principal: Principal,
    case: dict[str, Any],
    *,
    proposal_id: UUID,
    now: datetime,
    request_id: str | None,
) -> dict[str, Any]:
    """Act on a quoted proposal with the shopper's consent.

    Only the shopper who owns the case can do this: calling it is their consent. Everything
    is rechecked at this moment under the line lock (balance, eligibility, the rules in
    force now), and the result is one of: declined by a rule, sent to review, queued for
    payment, or a new amount that needs a fresh confirmation.
    """
    if principal.role != "shopper" or case["shopper_id"] != principal.shopper_id:
        raise Forbidden("Only the shopper who owns this request can confirm it")

    latest = conn.execute(
        "select * from proposals where case_id = %s order by version desc limit 1 for update",
        (case["id"],),
    ).fetchone()
    if latest is None or latest["id"] != proposal_id:
        known = conn.execute(
            "select 1 from proposals where id = %s and case_id = %s", (proposal_id, case["id"])
        ).fetchone()
        if known is None:
            raise NotFound()
        return _submit_result(case, None, outcome="stale_proposal", current=latest)

    if latest["status"] in ("submitted", "approved", "pending_review", "declined"):
        # Already acted on. Report where it stands; never act twice.
        return _submit_result(case, None, outcome="already_submitted", current=latest)
    if case["state"] not in REPLYABLE_STATES:
        raise Conflict("This case can no longer be submitted", details={"state": case["state"]})

    line = load_line_for_update(conn, case["order_id"], case["order_line_id"])
    if line is None:
        raise NotFound()
    action_id = _propose_and_route(
        conn,
        principal,
        case,
        line,
        consent=True,
        consented_amount_cents=latest["amount_cents"],
        now=now,
        request_id=request_id,
    )
    current = conn.execute(
        "select * from proposals where case_id = %s order by version desc limit 1",
        (case["id"],),
    ).fetchone()
    outcome = {
        "processing": "processing",
        "awaiting_supervisor": "pending_review",
        "declined": "declined",
        "awaiting_customer": "needs_confirmation",
    }.get(case["state"], case["state"])
    return _submit_result(case, action_id, outcome=outcome, current=current)


def _submit_result(
    case: dict[str, Any],
    action_id: UUID | None,
    *,
    outcome: str,
    current: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "case_state": case["state"],
        "action_id": action_id,
        "proposal_id": current["id"] if current else None,
        "proposal_version": current["version"] if current else None,
        "amount_cents": current["amount_cents"] if current else None,
    }


def ask(
    conn: Conn,
    case: dict[str, Any],
    *,
    text: str,
    kind: str,
    now: datetime,
    request_id: str | None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Ask the shopper one specific question and wait for the reply."""
    add_message(
        conn,
        merchant_id=case["merchant_id"],
        case_id=case["id"],
        author_kind="system",
        body=text,
        now=now,
    )
    if case["state"] != "awaiting_customer":
        move_case(
            conn,
            case,
            "awaiting_customer",
            kind=kind,
            actor_kind="system",
            request_id=request_id,
            detail=detail,
            now=now,
        )


def answer(
    conn: Conn,
    case: dict[str, Any],
    *,
    text: str,
    reason: str,
    now: datetime,
    request_id: str | None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Give an information-only answer and close the case as answered. A later message
    from the shopper reopens it."""
    add_message(
        conn,
        merchant_id=case["merchant_id"],
        case_id=case["id"],
        author_kind="system",
        body=text,
        now=now,
    )
    move_case(
        conn,
        case,
        "resolved",
        kind="answered",
        actor_kind="system",
        request_id=request_id,
        outcome_type="answered",
        outcome_reason=reason,
        resolved_at=now,
        detail=detail,
        now=now,
    )


def hand_to_staff(
    conn: Conn,
    case: dict[str, Any],
    *,
    text: str,
    reason: str,
    now: datetime,
    request_id: str | None,
) -> None:
    """Tell the shopper a person will look at this, and flag the open case for staff."""
    add_message(
        conn,
        merchant_id=case["merchant_id"],
        case_id=case["id"],
        author_kind="system",
        body=text,
        now=now,
    )
    record_event(
        conn,
        merchant_id=case["merchant_id"],
        case_id=case["id"],
        kind="needs_staff",
        actor_kind="system",
        request_id=request_id,
        detail={"reason": reason},
        now=now,
    )


def say(conn: Conn, case: dict[str, Any], *, text: str, now: datetime) -> None:
    """Add a message without changing the case."""
    add_message(
        conn,
        merchant_id=case["merchant_id"],
        case_id=case["id"],
        author_kind="system",
        body=text,
        now=now,
    )
