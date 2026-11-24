from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from returns_desk.clock import Clock
from returns_desk.db import Conn, Database, Principal
from returns_desk.domain.rules import Decision
from returns_desk.errors import Forbidden, InvalidRequest, NotFound
from returns_desk.services.common import (
    ACTIVE_STATES,
    add_message,
    format_usd,
    move_case,
    record_event,
)
from returns_desk.services.execution import authorize_and_queue, authorize_exchange
from returns_desk.services.quotes import (
    load_line_for_update,
    quote_for_case,
    variants_in_stock,
    write_proposal,
)

ReasonCode = Literal[
    "damaged", "wrong_item", "not_as_described", "does_not_fit", "changed_mind", "other"
]


class OpenCaseInput(BaseModel):
    """The fixed, typed request used until the agent arrives in milestone 2. It carries what
    the agent will later extract from plain English: the line, the quantity, the action and
    whether the shopper has clearly asked for it."""

    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=100)
    order_id: UUID
    order_line_id: UUID
    quantity: int = Field(default=1, ge=1, le=1000)
    action: Literal["refund", "exchange"] = "refund"
    # For an exchange: the variant of the same product the shopper wants instead.
    replacement_variant: str | None = Field(default=None, min_length=1, max_length=200)
    reason: ReasonCode
    message: str = Field(default="", max_length=4000)
    consent: bool = False

    @model_validator(mode="after")
    def _replacement_goes_with_exchange(self) -> OpenCaseInput:
        if (self.action == "exchange") != (self.replacement_variant is not None):
            raise ValueError("replacement_variant is required for an exchange and only then")
        return self


class MessageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=100)
    text: str = Field(default="", max_length=4000)
    consent: bool = False
    # A different replacement, when the first choice turned out to be out of stock.
    replacement_variant: str | None = Field(default=None, min_length=1, max_length=200)


@dataclass(frozen=True)
class CaseResult:
    case_id: UUID
    # Set when this call authorized an action that should now be executed.
    action_id: UUID | None = None
    # The same client request was seen before; nothing new was created.
    replayed: bool = False
    # The request was about units an existing case already covers.
    existing_resolution: bool = False


def open_case(
    db: Database,
    clock: Clock,
    principal: Principal,
    data: OpenCaseInput,
    request_id: str | None,
) -> CaseResult:
    if principal.role != "shopper" or principal.shopper_id is None:
        raise Forbidden("Only a signed-in shopper can open a request")
    now = clock.now()

    with db.tenant_tx(principal) as conn:
        replay = _case_by_client_request(conn, principal, data.client_request_id)
        if replay is not None:
            return CaseResult(case_id=replay, replayed=True)

        # Step 1, identify: row-level security decides whether this shopper may see the
        # line. Someone else's order looks exactly like an order that does not exist.
        line = load_line_for_update(conn, data.order_id, data.order_line_id)
        if line is None or line["shopper_id"] != principal.shopper_id:
            raise NotFound()

        # Checked again under the line lock, in case two tabs submitted at once.
        replay = _case_by_client_request(conn, principal, data.client_request_id)
        if replay is not None:
            return CaseResult(case_id=replay, replayed=True)

        existing = _existing_case_for_units(conn, line, data.quantity)
        if existing is not None:
            # A retry or a second mention of the same units: answer with the case that
            # already covers them instead of starting another refund.
            repeated = conn.execute(
                "select 1 from case_messages where case_id = %s and client_request_id = %s",
                (existing, data.client_request_id),
            ).fetchone()
            if data.message and repeated is None:
                add_message(
                    conn,
                    merchant_id=principal.merchant_id,
                    case_id=existing,
                    author_kind="shopper",
                    author_user_id=principal.user_id,
                    body=data.message,
                    client_request_id=data.client_request_id,
                    now=now,
                )
            record_event(
                conn,
                merchant_id=principal.merchant_id,
                case_id=existing,
                kind="repeat_request",
                actor_kind="shopper",
                actor_user_id=principal.user_id,
                request_id=request_id,
                detail={"client_request_id": data.client_request_id, "quantity": data.quantity},
                now=now,
            )
            return CaseResult(case_id=existing, existing_resolution=True)

        case = conn.execute(
            """
            insert into cases
              (merchant_id, shopper_id, client_request_id, order_id, order_line_id, quantity,
               requested_action, replacement_variant, reason_code, state,
               request_received_at, created_at, updated_at)
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'open', %s, %s, %s)
            returning *
            """,
            (
                principal.merchant_id,
                principal.shopper_id,
                data.client_request_id,
                line["order_id"],
                line["id"],
                data.quantity,
                data.action,
                data.replacement_variant,
                data.reason,
                now,
                now,
                now,
            ),
        ).fetchone()
        record_event(
            conn,
            merchant_id=principal.merchant_id,
            case_id=case["id"],
            kind="case_opened",
            to_state="open",
            actor_kind="shopper",
            actor_user_id=principal.user_id,
            request_id=request_id,
            detail={"client_request_id": data.client_request_id},
            now=now,
        )
        if data.message:
            add_message(
                conn,
                merchant_id=principal.merchant_id,
                case_id=case["id"],
                author_kind="shopper",
                author_user_id=principal.user_id,
                body=data.message,
                now=now,
            )
        action_id = _propose_and_route(
            conn,
            principal,
            case,
            line,
            consent=data.consent,
            consented_amount_cents=None,
            now=now,
            request_id=request_id,
        )
    return CaseResult(case_id=case["id"], action_id=action_id)


def post_message(
    db: Database,
    clock: Clock,
    principal: Principal,
    case_id: UUID,
    data: MessageInput,
    request_id: str | None,
) -> CaseResult:
    """Continue a typed case. A reply can give consent, and on an exchange it can name a
    different replacement."""
    if not data.text and not data.consent and not data.replacement_variant:
        raise InvalidRequest("A message needs text, a confirmation, or both")
    now = clock.now()
    with db.tenant_tx(principal) as conn:
        case = conn.execute("select * from cases where id = %s for update", (case_id,)).fetchone()
        if case is None:
            raise NotFound()

        seen = conn.execute(
            "select 1 from case_messages where case_id = %s and client_request_id = %s",
            (case_id, data.client_request_id),
        ).fetchone()
        if seen is not None:
            return CaseResult(case_id=case_id, replayed=True)

        add_message(
            conn,
            merchant_id=principal.merchant_id,
            case_id=case_id,
            author_kind=principal.role,
            author_user_id=principal.user_id,
            body=data.text or ("I confirm." if data.consent else ""),
            client_request_id=data.client_request_id,
            now=now,
        )

        action_id = None
        is_owner = principal.role == "shopper" and case["shopper_id"] == principal.shopper_id
        waiting = is_owner and case["state"] == "awaiting_customer"
        if waiting and data.replacement_variant and case["requested_action"] == "exchange":
            conn.execute(
                "update cases set replacement_variant = %s, updated_at = %s where id = %s",
                (data.replacement_variant, now, case_id),
            )
            case["replacement_variant"] = data.replacement_variant
        if waiting and (data.consent or data.replacement_variant):
            line = load_line_for_update(conn, case["order_id"], case["order_line_id"])
            if line is None:
                raise NotFound()
            shown = conn.execute(
                """
                select amount_cents from proposals
                where case_id = %s and status = 'awaiting_consent'
                order by version desc limit 1
                """,
                (case_id,),
            ).fetchone()
            action_id = _propose_and_route(
                conn,
                principal,
                case,
                line,
                consent=data.consent,
                consented_amount_cents=shown["amount_cents"] if shown else None,
                now=now,
                request_id=request_id,
            )
    return CaseResult(case_id=case_id, action_id=action_id)


# --- internals ------------------------------------------------------------------------


def _case_by_client_request(
    conn: Conn, principal: Principal, client_request_id: str
) -> UUID | None:
    row = conn.execute(
        "select id from cases where shopper_id = %s and client_request_id = %s",
        (principal.shopper_id, client_request_id),
    ).fetchone()
    return row["id"] if row else None


def _existing_case_for_units(
    conn: Conn, line: dict[str, Any], quantity: int, *, exclude_case_id: UUID | None = None
) -> UUID | None:
    """If the requested units are already spoken for, return the case that covers them.

    Units are spoken for when they are refunded or claimed by a case that is still in
    progress. A line with spare units returns None, so a second legitimate return on the
    same line gets its own case and its own action.
    """
    params = {"line": line["id"], "active": list(ACTIVE_STATES), "exclude": exclude_case_id}
    claimed = conn.execute(
        """
        select coalesce(sum(quantity), 0) as q from cases
        where order_line_id = %(line)s
          and state = any(%(active)s::case_state[])
          and (%(exclude)s::uuid is null or id <> %(exclude)s::uuid)
        """,
        params,
    ).fetchone()["q"]
    available = (
        line["quantity"] - line["refunded_quantity"] - line["exchanged_quantity"] - int(claimed)
    )
    if quantity <= available:
        return None
    # The case to point at: one still in progress if there is one, otherwise the one that
    # ended in a refund or an exchange. Declined and information-only cases cover no units.
    row = conn.execute(
        """
        select id from cases
        where order_line_id = %(line)s
          and (state = any(%(active)s::case_state[]) or outcome_type in ('refund', 'exchange'))
          and (%(exclude)s::uuid is null or id <> %(exclude)s::uuid)
        order by (state = 'resolved'), created_at desc
        limit 1
        """,
        params,
    ).fetchone()
    return row["id"] if row else None


def _propose_and_route(
    conn: Conn,
    principal: Principal,
    case: dict[str, Any],
    line: dict[str, Any],
    *,
    consent: bool,
    consented_amount_cents: int | None,
    now: datetime,
    request_id: str | None,
) -> UUID | None:
    """Steps 3 to 5: gather evidence, decide in code, then act or pause.

    Returns the action ID when a refund was authorized and queued for the payment
    processor, otherwise None. An exchange is carried out whole in this transaction and
    leaves nothing to queue.
    """
    exchange = case["requested_action"] == "exchange"
    # With consent in hand this call may act, so the stock count it reads is locked.
    quote = quote_for_case(
        conn,
        case,
        line,
        merchant_timezone=principal.merchant_timezone,
        now=now,
        lock_stock=consent and exchange,
    )
    ev = quote.evaluation
    actor = {"actor_kind": "system", "request_id": request_id, "now": now}
    item = f"{line['description']} ({line['variant']})" if line["variant"] else line["description"]

    if ev.decision is Decision.INELIGIBLE and replacement_is_the_only_problem(ev.hard_reasons):
        offer_another_replacement(conn, case, line, quote, request_id=request_id, now=now)
        return None

    if ev.decision is Decision.INELIGIBLE:
        proposal = write_proposal(
            conn, case=case, quote=quote, status="ineligible", now=now, consented_at=None
        )
        move_case(
            conn,
            case,
            "declined",
            kind="declined_by_rule",
            outcome_type="declined_ineligible",
            outcome_reason=ev.hard_reasons[0],
            detail={"proposal_id": str(proposal["id"]), "reasons": list(ev.hard_reasons)},
            **actor,
        )
        add_message(
            conn,
            merchant_id=case["merchant_id"],
            case_id=case["id"],
            author_kind="system",
            body=_DECLINE_WORDING.get(ev.hard_reasons[0], "This request is not eligible."),
            now=now,
        )
        return None

    # Consent must match what will actually happen. If the amount moved since the shopper
    # said yes, ask again.
    if consent and consented_amount_cents is not None and consented_amount_cents != ev.amount_cents:
        consent = False

    if not consent:
        proposal = write_proposal(
            conn, case=case, quote=quote, status="awaiting_consent", now=now, consented_at=None
        )
        move_case(
            conn,
            case,
            "awaiting_customer",
            kind="consent_requested",
            detail={"proposal_id": str(proposal["id"]), "version": proposal["version"]},
            **actor,
        )
        add_message(
            conn,
            merchant_id=case["merchant_id"],
            case_id=case["id"],
            author_kind="system",
            body=(
                f"Please confirm: exchange {case['quantity']} x {item} for "
                f"{case['replacement_variant']}?"
                if exchange
                else f"Please confirm: refund {format_usd(ev.amount_cents)} for "
                f"{case['quantity']} x {item}?"
            ),
            now=now,
        )
        return None

    if ev.decision is Decision.REVIEW:
        proposal = write_proposal(
            conn, case=case, quote=quote, status="pending_review", now=now, consented_at=now
        )
        move_case(
            conn,
            case,
            "awaiting_supervisor",
            kind="sent_to_review",
            # The reasons stay out of the event detail too: associates can read events.
            detail={"proposal_id": str(proposal["id"]), "version": proposal["version"]},
            **actor,
        )
        add_message(
            conn,
            merchant_id=case["merchant_id"],
            case_id=case["id"],
            author_kind="system",
            body="Thanks. Your request is with our team for review. We will update you here.",
            now=now,
        )
        return None

    proposal = write_proposal(
        conn, case=case, quote=quote, status="submitted", now=now, consented_at=now
    )
    operational_version_id = quote.operational.version_id if quote.operational else None
    if exchange:
        done = authorize_exchange(
            conn,
            case=case,
            proposal=proposal,
            line=line,
            operational_version_id=operational_version_id,
            now=now,
            request_id=request_id,
        )
        finish_exchange(
            conn,
            case,
            proposal,
            done,
            authorized_by="automatic",
            actor_kind="system",
            actor_user_id=None,
            request_id=request_id,
            now=now,
        )
        return None

    action_id = authorize_and_queue(
        conn,
        case=case,
        proposal=proposal,
        line=line,
        operational_version_id=operational_version_id,
        now=now,
        request_id=request_id,
    )
    move_case(
        conn,
        case,
        "processing",
        kind="refund_authorized",
        detail={
            "proposal_id": str(proposal["id"]),
            "action_id": str(action_id),
            "amount_cents": ev.amount_cents,
            "authorized_by": "automatic",
        },
        **actor,
    )
    return action_id


def replacement_is_the_only_problem(hard_reasons: tuple[str, ...]) -> bool:
    """True when an exchange fails only because the wanted variant is out of stock or does
    not exist. That is not a reason to close the request: the shopper gets another choice."""
    return bool(hard_reasons) and set(hard_reasons) <= {
        "replacement_unavailable",
        "replacement_unknown",
    }


def offer_another_replacement(
    conn: Conn,
    case: dict[str, Any],
    line: dict[str, Any],
    quote: Any,
    *,
    request_id: str | None,
    now: datetime,
) -> list[str]:
    """The wanted replacement cannot be had. Record that, leave every balance alone, and
    put the choice back with the shopper: another variant that is in stock, or a refund.
    Returns the variants on offer."""
    proposal = write_proposal(
        conn, case=case, quote=quote, status="ineligible", now=now, consented_at=None
    )
    options = variants_in_stock(conn, sku=line["sku"], quantity=case["quantity"])
    wanted = case.get("replacement_variant") or "that version"
    if options:
        text = (
            f"Sorry, {line['description']} in {wanted} is not available. "
            f"In stock: {', '.join(options)}. Tell us which one you would like instead, "
            "or ask for a refund."
        )
    else:
        text = (
            f"Sorry, {line['description']} in {wanted} is not available and no other "
            "version is in stock. Would you like a refund instead?"
        )
    detail = {
        "proposal_id": str(proposal["id"]),
        "wanted": case.get("replacement_variant"),
        "alternatives": options,
    }
    if case["state"] == "awaiting_customer":
        record_event(
            conn,
            merchant_id=case["merchant_id"],
            case_id=case["id"],
            kind="replacement_unavailable",
            actor_kind="system",
            request_id=request_id,
            detail=detail,
            now=now,
        )
    else:
        move_case(
            conn,
            case,
            "awaiting_customer",
            kind="replacement_unavailable",
            actor_kind="system",
            request_id=request_id,
            detail=detail,
            now=now,
        )
    add_message(
        conn,
        merchant_id=case["merchant_id"],
        case_id=case["id"],
        author_kind="system",
        body=text,
        now=now,
    )
    return options


def finish_exchange(
    conn: Conn,
    case: dict[str, Any],
    proposal: dict[str, Any],
    done: dict[str, Any],
    *,
    authorized_by: str,
    actor_kind: str,
    actor_user_id: UUID | None,
    request_id: str | None,
    now: datetime,
) -> None:
    """Close a case whose exchange has just been carried out in this transaction."""
    move_case(
        conn,
        case,
        "resolved",
        kind="exchange_confirmed",
        actor_kind=actor_kind,
        actor_user_id=actor_user_id,
        request_id=request_id,
        outcome_type="exchange",
        resolved_at=now,
        detail={
            "proposal_id": str(proposal["id"]),
            "action_id": str(done["action_id"]),
            "fulfillment_reference": done["reference"],
            "replacement_variant": proposal["replacement_variant"],
            "authorized_by": authorized_by,
        },
        now=now,
    )
    add_message(
        conn,
        merchant_id=case["merchant_id"],
        case_id=case["id"],
        author_kind="system",
        body=(
            f"Your exchange is confirmed. We have set aside {proposal['quantity']} x "
            f"{proposal['replacement_variant']} for you. Reference: {done['reference']}."
        ),
        now=now,
    )


_DECLINE_WORDING = {
    "outside_return_window": (
        "This item is outside the return window in the policy you bought under."
    ),
    "quantity_unavailable": (
        "These units have already been returned or are part of another request."
    ),
    "invalid_quantity": "The quantity in this request is not valid.",
}
