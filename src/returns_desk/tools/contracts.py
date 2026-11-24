"""The tool contracts.

These are the only operations the agent has. Each one takes the caller's verified
principal from the server, never from tool arguments, checks authority again on every
call, validates typed inputs, and returns a structured result. An order or case the
caller may not see produces exactly the same error as one that does not exist.

    get_order               read purchased lines, paid amounts, what is still refundable
    search_policy           ranked policy passages the caller is allowed to read
    quote_resolution        compute a resolution; changes no financial state
    submit_resolution       act on a quote with the shopper's consent (idempotent)
    get_resolution_status   read the current outcome and permitted receipt fields

check_inventory arrives with exchanges in the stretch milestone. The supervisor decision
is deliberately not a tool: it is a separate authenticated API the agent cannot reach.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from pydantic import BaseModel

from returns_desk.db import Principal
from returns_desk.errors import InvalidRequest, NotFound
from returns_desk.retrieval.search import applicable_version_ids
from returns_desk.services import chat, views

if TYPE_CHECKING:
    from returns_desk.container import Container

Reason = Literal["damaged", "wrong_item", "not_as_described", "changed_mind", "other"]


class LineOut(BaseModel):
    line_id: str
    sku: str
    description: str
    variant: str
    quantity: int
    unit_paid_cents: int
    remaining_quantity: int
    remaining_cents: int


class ActionOut(BaseModel):
    action_id: str
    case_id: str
    line_id: str
    kind: str
    status: str
    quantity: int
    amount_cents: int


class OpenCaseOut(BaseModel):
    case_id: str
    line_id: str
    state: str


class OrderOut(BaseModel):
    order_id: str
    order_number: str
    purchased_at: str
    delivered_at: str | None
    currency: str
    lines: list[LineOut]
    actions: list[ActionOut]
    open_cases: list[OpenCaseOut]


class OrdersResult(BaseModel):
    orders: list[OrderOut]


class PassageOut(BaseModel):
    passage_id: str
    version_id: str
    version_label: str
    section_key: str
    title: str
    audience: str
    text: str
    effective_from: str
    effective_to: str | None
    score: float


class PolicyResult(BaseModel):
    passages: list[PassageOut]
    # False when no single policy version applies. The caller must then say the policy
    # needs verification instead of guessing.
    policy_found: bool


class CitationOut(BaseModel):
    version_label: str
    section_key: str
    title: str
    body: str


class ItemOut(BaseModel):
    order_number: str
    description: str
    variant: str
    quantity: int


class QuoteResult(BaseModel):
    proposal_id: str
    proposal_version: int
    eligible: bool
    amount_cents: int
    currency: str
    requires_review: bool
    reason_codes: list[str]
    evidence: dict[str, Any]
    citations: list[CitationOut]
    existing_case_id: str | None
    item: ItemOut


class SubmitResult(BaseModel):
    outcome: str
    case_state: str
    action_id: str | None
    proposal_id: str | None
    proposal_version: int | None
    amount_cents: int | None


class ReceiptOut(BaseModel):
    refund_reference: str
    amount_cents: int
    currency: str
    confirmed_at: str


class StatusResult(BaseModel):
    case_id: str
    state: str
    status: str
    status_message: str
    amount_cents: int | None
    item: ItemOut | None
    action_status: str | None
    receipt: ReceiptOut | None
    outcome_type: str | None
    outcome_reason: str | None


def _uuid(value: str, what: str) -> UUID:
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        # A malformed reference is treated like an unknown one.
        raise NotFound() from None


def get_order(
    container: Container, principal: Principal, *, case_id: str, order_number: str | None = None
) -> OrdersResult:
    with container.db.tenant_tx(principal) as conn:
        case = chat.load_case(conn, _uuid(case_id, "case"))
        orders = chat.orders_for_case(conn, case, order_number=order_number)
    return OrdersResult(
        orders=[
            OrderOut(
                order_id=str(o["id"]),
                order_number=o["order_number"],
                purchased_at=o["purchased_at"].isoformat(),
                delivered_at=o["delivered_at"].isoformat() if o["delivered_at"] else None,
                currency=o["currency"],
                lines=[
                    LineOut(
                        line_id=str(line["id"]),
                        sku=line["sku"],
                        description=line["description"],
                        variant=line["variant"],
                        quantity=line["quantity"],
                        unit_paid_cents=line["unit_paid_cents"],
                        remaining_quantity=line["remaining_quantity"],
                        remaining_cents=line["remaining_cents"],
                    )
                    for line in o["lines"]
                ],
                actions=[
                    ActionOut(
                        action_id=str(a["id"]),
                        case_id=str(a["case_id"]),
                        line_id=str(a["order_line_id"]),
                        kind=a["kind"],
                        status=a["status"],
                        quantity=a["quantity"],
                        amount_cents=a["amount_cents"],
                    )
                    for a in o["actions"]
                ],
                open_cases=[
                    OpenCaseOut(
                        case_id=str(c["id"]), line_id=str(c["order_line_id"]), state=c["state"]
                    )
                    for c in o["open_cases"]
                ],
            )
            for o in orders
        ]
    )


def search_policy(
    container: Container, principal: Principal, *, case_id: str, question: str, k: int = 5
) -> PolicyResult:
    if not question.strip():
        raise InvalidRequest("A question is required")
    k = max(1, min(k, 10))
    now = container.clock.now()
    with container.db.tenant_tx(principal) as conn:
        case = chat.load_case(conn, _uuid(case_id, "case"))
        purchased_at = None
        if case["order_id"] is not None:
            order = conn.execute(
                "select purchased_at from orders where id = %s", (case["order_id"],)
            ).fetchone()
            purchased_at = order["purchased_at"] if order else None
        # The applicable date comes from the order, not from the caller.
        version_ids = applicable_version_ids(conn, purchased_at=purchased_at, now=now)
        passages = container.policy_search.search(
            conn, principal, question=question, version_ids=version_ids, k=k
        )
    return PolicyResult(
        passages=[PassageOut(**p.as_dict()) for p in passages],
        policy_found=bool(version_ids),
    )


def quote_resolution(
    container: Container,
    principal: Principal,
    *,
    case_id: str,
    order_line_id: str,
    quantity: int,
    action: Literal["refund"] = "refund",
    reason: Reason,
) -> QuoteResult:
    now = container.clock.now()
    with container.db.tenant_tx(principal) as conn:
        case = chat.load_case(conn, _uuid(case_id, "case"), lock=True)
        result = chat.quote(
            conn,
            principal,
            case,
            order_line_id=_uuid(order_line_id, "line"),
            quantity=quantity,
            reason=reason,
            now=now,
        )
    return QuoteResult(
        proposal_id=str(result["proposal_id"]),
        proposal_version=result["proposal_version"],
        eligible=result["eligible"],
        amount_cents=result["amount_cents"],
        currency=result["currency"],
        requires_review=result["requires_review"],
        reason_codes=result["reason_codes"],
        evidence=result["evidence"],
        citations=[CitationOut(**c) for c in result["citations"]],
        existing_case_id=str(result["existing_case_id"]) if result["existing_case_id"] else None,
        item=ItemOut(**result["item"]),
    )


def submit_resolution(
    container: Container,
    principal: Principal,
    *,
    case_id: str,
    proposal_id: str,
    request_id: str | None = None,
) -> SubmitResult:
    now = container.clock.now()
    with container.db.tenant_tx(principal) as conn:
        case = chat.load_case(conn, _uuid(case_id, "case"), lock=True)
        result = chat.submit(
            conn,
            principal,
            case,
            proposal_id=_uuid(proposal_id, "proposal"),
            now=now,
            request_id=request_id,
        )
    if result["action_id"] is not None:
        # The authorization is committed. Try the payment once now; if this fails for any
        # reason the reconcile job picks the action up from the outbox.
        with contextlib.suppress(Exception):
            container.execution.process(result["action_id"])
        with container.db.tenant_tx(principal) as conn:
            state = conn.execute(
                "select state::text as state from cases where id = %s", (case["id"],)
            ).fetchone()
            result["case_state"] = state["state"] if state else result["case_state"]
    return SubmitResult(
        outcome=result["outcome"],
        case_state=result["case_state"],
        action_id=str(result["action_id"]) if result["action_id"] else None,
        proposal_id=str(result["proposal_id"]) if result["proposal_id"] else None,
        proposal_version=result["proposal_version"],
        amount_cents=result["amount_cents"],
    )


def get_resolution_status(
    container: Container, principal: Principal, *, case_id: str
) -> StatusResult:
    with container.db.tenant_tx(principal) as conn:
        view = views.case_view(conn, principal, _uuid(case_id, "case"))
    if view is None:
        raise NotFound()
    item = view["item"]
    receipt = view["receipt"]
    outcome = view["outcome"] or {}
    return StatusResult(
        case_id=str(view["id"]),
        state=view["state"],
        status=view["status"],
        status_message=view["status_message"],
        amount_cents=view["amount_cents"],
        item=(
            ItemOut(
                order_number=item["order_number"],
                description=item["description"],
                variant=item["variant"],
                quantity=item["quantity"],
            )
            if item and item.get("order_line_id")
            else None
        ),
        action_status=view["action"]["status"] if view["action"] else None,
        receipt=(
            ReceiptOut(
                refund_reference=receipt["refund_reference"],
                amount_cents=receipt["amount_cents"],
                currency=receipt["currency"],
                confirmed_at=receipt["confirmed_at"].isoformat(),
            )
            if receipt
            else None
        ),
        outcome_type=outcome.get("type"),
        outcome_reason=outcome.get("reason"),
    )
