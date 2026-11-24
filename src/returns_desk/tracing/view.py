"""Reading a case's trace through the API.

The rows are read under the caller's row-level security, so what a staff member sees is
what their role may see today. A supervisor who becomes an associate loses the restricted
review reasons on old traces as well as new ones, because nothing here is stored per
reader: the trace is rebuilt from the audit rows on every read.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from returns_desk.db import Conn, Principal
from returns_desk.errors import Forbidden
from returns_desk.tracing.spans import (
    Span,
    span_from_action_attempt,
    span_from_model_attempt,
    spans_from_event,
    totals,
    trace_id,
)

EVENTS_SQL = """
    select e.id, e.case_id, e.kind, e.from_state::text as from_state,
           e.to_state::text as to_state, e.actor_kind, e.request_id, e.detail, e.created_at,
           -- When the case entered the state this event moves it out of.
           (select p.created_at from case_events p
             where p.case_id = e.case_id and p.id < e.id and p.to_state = e.from_state
             order by p.id desc limit 1) as entered_at
    from case_events e
"""

MODEL_ATTEMPTS_SQL = """
    select a.id, a.case_id, a.turn, a.purpose, a.route, a.model, a.tier, a.outcome,
           a.input_tokens, a.output_tokens, a.latency_ms, a.error, a.request_id, a.created_at
    from model_attempts a
"""

ACTION_ATTEMPTS_SQL = """
    select aa.id, aa.action_id, aa.attempt_no, aa.operation, aa.outcome, aa.detail, aa.at,
           a.case_id, a.kind as action_kind, a.request_id
    from action_attempts aa
    join resolution_actions a on a.id = aa.action_id
"""


def build_spans(
    events: list[dict[str, Any]],
    model_attempts: list[dict[str, Any]],
    action_attempts: list[dict[str, Any]],
    turn_requests: set[tuple[UUID, str]],
) -> list[Span]:
    spans: list[Span] = []
    for row in events:
        spans.extend(spans_from_event(row, turn_requests))
    spans.extend(span_from_model_attempt(row, turn_requests) for row in model_attempts)
    spans.extend(span_from_action_attempt(row) for row in action_attempts)
    return spans


def in_reading_order(spans: list[Span]) -> list[Span]:
    """Top-level spans by start time, each followed by its own children by start time.
    Where times are equal, the order the audit rows were written in decides."""
    written = {id(span): index for index, span in enumerate(spans)}

    def by_time(span: Span) -> tuple[Any, int]:
        return (span.start, written[id(span)])

    known = {span.span_id for span in spans}
    children: dict[str, list[Span]] = {}
    roots: list[Span] = []
    for span in spans:
        if span.parent_id and span.parent_id in known:
            children.setdefault(span.parent_id, []).append(span)
        else:
            roots.append(span)
    ordered: list[Span] = []
    for root in sorted(roots, key=by_time):
        ordered.append(root)
        ordered.extend(sorted(children.get(root.span_id, []), key=by_time))
    return ordered


def case_trace(conn: Conn, principal: Principal, case_id: UUID) -> dict[str, Any] | None:
    """The trace of one case for a staff member, or None if they cannot see the case."""
    if not principal.is_staff:
        raise Forbidden("Only staff can read a trace")
    case = conn.execute(
        """
        select id, state::text as state, outcome_type, channel, created_at, resolved_at
        from cases where id = %s
        """,
        (case_id,),
    ).fetchone()
    if case is None:
        return None

    events = conn.execute(EVENTS_SQL + " where e.case_id = %s order by e.id", (case_id,)).fetchall()
    model_attempts = conn.execute(
        MODEL_ATTEMPTS_SQL + " where a.case_id = %s order by a.id", (case_id,)
    ).fetchall()
    action_attempts = conn.execute(
        ACTION_ATTEMPTS_SQL + " where a.case_id = %s order by aa.id", (case_id,)
    ).fetchall()
    turn_requests = {
        (e["case_id"], e["request_id"])
        for e in events
        if e["kind"] == "agent_turn" and e["request_id"]
    }
    spans = in_reading_order(build_spans(events, model_attempts, action_attempts, turn_requests))

    request_ids = list(
        dict.fromkeys(
            span.attributes["request_id"] for span in spans if span.attributes.get("request_id")
        )
    )
    trace: dict[str, Any] = {
        "trace_id": trace_id(case_id),
        "case_id": case["id"],
        "state": case["state"],
        "outcome_type": case["outcome_type"],
        "channel": case["channel"],
        "started_at": case["created_at"],
        "resolved_at": case["resolved_at"],
        # One ID per HTTP request that touched the case, in order.
        "request_ids": request_ids,
        "totals": totals(spans),
        "spans": [span.as_dict() for span in spans],
    }
    if principal.role == "supervisor":
        # review_records is supervisor-only in the database. An associate's query would
        # come back empty; the key is left out for them so the shape says so too.
        trace["review"] = conn.execute(
            """
            select p.version as proposal_version, rr.reasons, rr.internal_evidence
            from review_records rr
            join proposals p on p.id = rr.proposal_id
            where p.case_id = %s
            order by p.version
            """,
            (case_id,),
        ).fetchall()
    return trace
