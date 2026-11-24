from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from returns_desk.clock import Clock
from returns_desk.db import Conn, Database, Principal
from returns_desk.domain.rules import Decision
from returns_desk.errors import Conflict, Forbidden, NotFound
from returns_desk.services import policies
from returns_desk.services.common import add_message, move_case, record_event
from returns_desk.services.execution import authorize_and_queue
from returns_desk.services.quotes import load_line_for_update, quote_refund, write_proposal


class DecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "decline"]
    note: str = Field(default="", max_length=2000)


class ReviewRequestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str = Field(min_length=1, max_length=2000)


@dataclass(frozen=True)
class DecisionResult:
    body: dict[str, Any]
    action_id: UUID | None = None


def list_reviews(db: Database, principal: Principal) -> list[dict[str, Any]]:
    """Pending exceptions for the supervisor's merchant, with everything needed to decide:
    amount, proposed action, the applicable customer policy, the internal rule and the reason."""
    _require_supervisor(principal)
    with db.tenant_tx(principal) as conn:
        rows = conn.execute(
            """
            select p.id as review_id, p.version as proposal_version, p.case_id,
                   p.action_kind as proposed_action, p.quantity, p.amount_cents,
                   p.customer_policy_version_id, p.operational_policy_version_id,
                   p.cited_sections, p.evidence, p.created_at,
                   c.shopper_id, c.reason_code, c.request_received_at,
                   ol.description, ol.variant, o.order_number,
                   rr.reasons, rr.internal_evidence
            from proposals p
            join cases c on c.id = p.case_id
            join order_lines ol on ol.id = p.order_line_id
            join orders o on o.id = ol.order_id
            left join review_records rr on rr.proposal_id = p.id
            where p.status = 'pending_review' and c.state = 'awaiting_supervisor'
            order by p.created_at, p.id
            """
        ).fetchall()
        return [_review_view(conn, row) for row in rows]


def request_review(
    db: Database,
    clock: Clock,
    principal: Principal,
    case_id: UUID,
    data: ReviewRequestInput,
    request_id: str | None,
) -> bool:
    """A staff member sends a case to a supervisor.

    It changes no balance and makes no decision. It marks the case, and from then on the
    rules treat it as a review case whatever the amount: when the shopper confirms, the
    proposal goes to a supervisor instead of being paid automatically. The note is kept as
    an internal message the shopper never sees. Returns False when the case was already
    marked, so a second click changes nothing.
    """
    if not principal.is_staff:
        raise Forbidden("Only staff can send a case to review")
    now = clock.now()
    with db.tenant_tx(principal) as conn:
        case = conn.execute("select * from cases where id = %s for update", (case_id,)).fetchone()
        if case is None:
            raise NotFound()
        if case["review_requested_at"] is not None:
            return False
        if case["state"] not in ("open", "awaiting_customer"):
            raise Conflict(
                "This case can no longer be sent to review", details={"state": case["state"]}
            )
        conn.execute(
            """
            update cases
            set review_requested_at = %s, review_requested_by = %s, updated_at = %s
            where id = %s
            """,
            (now, principal.user_id, now, case_id),
        )
        add_message(
            conn,
            merchant_id=case["merchant_id"],
            case_id=case_id,
            author_kind=principal.role,
            author_user_id=principal.user_id,
            audience="internal",
            body=data.note,
            now=now,
        )
        record_event(
            conn,
            merchant_id=case["merchant_id"],
            case_id=case_id,
            kind="review_requested",
            actor_kind=principal.role,
            actor_user_id=principal.user_id,
            request_id=request_id,
            now=now,
        )
    return True


def decide(
    db: Database,
    clock: Clock,
    principal: Principal,
    review_id: UUID,
    data: DecisionInput,
    request_id: str | None,
) -> DecisionResult:
    """Record a supervisor's decision on one proposal version and resume the case.

    Safe to call twice and safe under a race: the proposal row is locked, and approvals has
    one row per proposal, so there is exactly one final decision. A second call returns the
    recorded decision, whatever it asked for.
    """
    _require_supervisor(principal)
    now = clock.now()
    stale: dict[str, Any] | None = None
    blocked: str | None = None

    with db.tenant_tx(principal) as conn:
        proposal = conn.execute(
            "select * from proposals where id = %s for update", (review_id,)
        ).fetchone()
        if proposal is None:
            # Unknown, or another merchant's: the two look the same.
            raise NotFound()

        recorded = conn.execute(
            "select * from approvals where proposal_id = %s", (review_id,)
        ).fetchone()
        if recorded is not None:
            return DecisionResult(body=_decision_body(proposal, recorded, replayed=True))

        if proposal["status"] != "pending_review":
            raise Conflict(
                "This proposal is no longer waiting for a decision",
                details={"current": _current_proposal(conn, proposal["case_id"])},
            )
        case = conn.execute(
            "select * from cases where id = %s for update", (proposal["case_id"],)
        ).fetchone()
        if case["state"] != "awaiting_supervisor":
            raise Conflict("This case is not waiting for a supervisor")

        if data.decision == "decline":
            approval = _record(conn, principal, proposal, "declined", data.note, request_id, now)
            conn.execute("update proposals set status = 'declined' where id = %s", (review_id,))
            move_case(
                conn,
                case,
                "declined",
                kind="declined_by_supervisor",
                actor_kind="supervisor",
                actor_user_id=principal.user_id,
                request_id=request_id,
                outcome_type="declined_by_supervisor",
                detail={"proposal_id": str(review_id), "version": proposal["version"]},
                now=now,
            )
            add_message(
                conn,
                merchant_id=case["merchant_id"],
                case_id=case["id"],
                author_kind="system",
                body="We reviewed your request and were not able to approve it.",
                now=now,
            )
            return DecisionResult(body=_decision_body(proposal, approval, replayed=False))

        # Approve. Recheck everything at the moment of authorization, under the line lock:
        # the balance, the hard constraints and the rules in force right now.
        line = load_line_for_update(conn, case["order_id"], case["order_line_id"])
        if line is None:
            raise NotFound()
        quote = quote_refund(
            conn,
            line=line,
            quantity=case["quantity"],
            reason=case["reason_code"],
            requested_at=case["request_received_at"],
            merchant_timezone=principal.merchant_timezone,
            now=now,
            exclude_case_id=case["id"],
            staff_escalated=case.get("review_requested_at") is not None,
        )
        ev = quote.evaluation
        seen = conn.execute(
            "select reasons from review_records where proposal_id = %s", (review_id,)
        ).fetchone()
        seen_reasons = set(seen["reasons"]) if seen else set()

        if ev.decision is Decision.INELIGIBLE:
            # A hard constraint, such as an already refunded unit, cannot be approved away.
            blocked = ev.hard_reasons[0]
            conn.execute("update proposals set status = 'ineligible' where id = %s", (review_id,))
            move_case(
                conn,
                case,
                "declined",
                kind="declined_by_rule",
                actor_kind="system",
                request_id=request_id,
                outcome_type="declined_ineligible",
                outcome_reason=blocked,
                detail={"proposal_id": str(review_id), "reasons": list(ev.hard_reasons)},
                now=now,
            )
        elif ev.amount_cents != proposal["amount_cents"] or set(ev.review_reasons) - seen_reasons:
            # What would execute is no longer what this supervisor was shown. Write a new
            # version and ask for a decision on that one.
            fresh = write_proposal(
                conn,
                case=case,
                quote=quote,
                status="pending_review",
                now=now,
                consented_at=proposal["consented_at"],
            )
            stale = {
                "review_id": str(fresh["id"]),
                "proposal_version": fresh["version"],
                "status": fresh["status"],
            }
        else:
            if proposal["consented_at"] is None:
                raise Conflict("The shopper has not confirmed this request")
            approval = _record(conn, principal, proposal, "approved", data.note, request_id, now)
            conn.execute("update proposals set status = 'approved' where id = %s", (review_id,))
            action_id = authorize_and_queue(
                conn,
                case=case,
                proposal=proposal,
                line=line,
                operational_version_id=(
                    quote.operational.version_id if quote.operational else None
                ),
                now=now,
                request_id=request_id,
            )
            move_case(
                conn,
                case,
                "processing",
                kind="refund_authorized",
                actor_kind="supervisor",
                actor_user_id=principal.user_id,
                request_id=request_id,
                detail={
                    "proposal_id": str(review_id),
                    "version": proposal["version"],
                    "action_id": str(action_id),
                    "amount_cents": proposal["amount_cents"],
                    "authorized_by": "supervisor",
                },
                now=now,
            )
            return DecisionResult(
                body=_decision_body(proposal, approval, replayed=False), action_id=action_id
            )

    # Reported after the transaction above has committed.
    if blocked is not None:
        raise Conflict(
            "This request can no longer be approved",
            details={"hard_constraint": blocked, "case_state": "declined"},
        )
    raise Conflict(
        "The proposal changed since it was shown. Review the current version.",
        details={"current": stale},
    )


# --- internals ------------------------------------------------------------------------


def _require_supervisor(principal: Principal) -> None:
    if principal.role != "supervisor":
        raise Forbidden("Only a supervisor can review exceptions")


def _record(
    conn: Conn,
    principal: Principal,
    proposal: dict[str, Any],
    decision: str,
    note: str,
    request_id: str | None,
    now: Any,
) -> dict[str, Any]:
    conn.execute(
        """
        insert into approvals
          (merchant_id, proposal_id, proposal_version, reviewer_user_id, decision, note,
           request_id, decided_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            principal.merchant_id,
            proposal["id"],
            proposal["version"],
            principal.user_id,
            decision,
            note,
            request_id,
            now,
        ),
    )
    return {
        "decision": decision,
        "reviewer_user_id": principal.user_id,
        "proposal_version": proposal["version"],
        "note": note,
        "decided_at": now,
    }


def _decision_body(
    proposal: dict[str, Any], approval: dict[str, Any], *, replayed: bool
) -> dict[str, Any]:
    return {
        "review_id": proposal["id"],
        "case_id": proposal["case_id"],
        "proposal_version": approval["proposal_version"],
        "decision": approval["decision"],
        "reviewer_user_id": approval["reviewer_user_id"],
        "note": approval["note"],
        "decided_at": approval["decided_at"],
        # True when this call changed nothing and is reporting an earlier decision.
        "already_decided": replayed,
    }


def _current_proposal(conn: Conn, case_id: UUID) -> dict[str, Any]:
    row = conn.execute(
        """
        select id, version, status from proposals
        where case_id = %s order by version desc limit 1
        """,
        (case_id,),
    ).fetchone()
    return {
        "review_id": str(row["id"]),
        "proposal_version": row["version"],
        "status": row["status"],
    }


def _review_view(conn: Conn, row: dict[str, Any]) -> dict[str, Any]:
    applicable = []
    if row["customer_policy_version_id"]:
        applicable = policies.sections_for(
            conn, row["customer_policy_version_id"], row["cited_sections"]
        )
    internal_rule = None
    if row["operational_policy_version_id"]:
        internal_rule = conn.execute(
            """
            select pv.id as version_id, pv.version_label, pv.rules, pv.effective_from
            from policy_versions pv where pv.id = %s
            """,
            (row["operational_policy_version_id"],),
        ).fetchone()
        if internal_rule is not None:
            internal_rule["sections"] = conn.execute(
                """
                select section_key, title, body from policy_sections
                where policy_version_id = %s order by section_key
                """,
                (row["operational_policy_version_id"],),
            ).fetchall()
    return {
        "review_id": row["review_id"],
        "proposal_version": row["proposal_version"],
        "case_id": row["case_id"],
        "proposed_action": row["proposed_action"],
        "item": {
            "order_number": row["order_number"],
            "description": row["description"],
            "variant": row["variant"],
            "quantity": row["quantity"],
        },
        "amount_cents": row["amount_cents"],
        "currency": "USD",
        "request_reason": row["reason_code"],
        "requested_at": row["request_received_at"],
        "reasons": row["reasons"] or [],
        "internal_evidence": row["internal_evidence"] or {},
        "applicable_policy": [
            {
                "version_label": s["version_label"],
                "section_key": s["section_key"],
                "title": s["title"],
                "body": s["body"],
            }
            for s in applicable
        ],
        "internal_rule": internal_rule,
        "evidence": row["evidence"],
        "created_at": row["created_at"],
    }
