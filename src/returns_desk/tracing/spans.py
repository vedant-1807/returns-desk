"""Traces, built from the audit trail.

Nothing is traced twice. The rows the service already writes for audit (case events,
model attempts, payment processor attempts) are the trace; this module shapes them into
spans. One case is one trace. The spans of one HTTP request share that request's ID, so a
single correlation ID runs from the shopper's message through the workflow, the tools,
retrieval, the model gateway, the approval and the processor call.

What a span may carry is decided here, once:

* IDs and outcome codes, never content. No message text, no policy text, no amounts typed
  by a person. A passage is an evidence ID; an order is a count.
* Time the service spent working is kept apart from time spent waiting for a person.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from returns_desk.models.pricing import PRICING_VERSION, list_price_usd

# Case states in which the clock is running on a person, not on the service.
HUMAN_WAITS = {"awaiting_customer": "customer", "awaiting_supervisor": "supervisor"}


@dataclass(frozen=True)
class Span:
    trace_id: str
    span_id: str
    parent_id: str | None
    # agent | generation | tool | retriever | event | wait
    kind: str
    name: str
    start: datetime
    end: datetime
    attributes: dict[str, Any] = field(default_factory=dict)
    level: str = "DEFAULT"
    status_message: str | None = None
    # The audit row this span was built from, as (table, id).
    source: tuple[str, int] = ("", 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "parent_id": self.parent_id,
            "kind": self.kind,
            "name": self.name,
            "start": self.start,
            "end": self.end,
            "ms": int((self.end - self.start).total_seconds() * 1000),
            "level": self.level,
            "status_message": self.status_message,
            "attributes": self.attributes,
        }


def trace_id(case_id: UUID) -> str:
    """A case is a trace. A UUID is exactly the 16 bytes a trace ID needs."""
    return case_id.hex


def _span_id(*parts: Any) -> str:
    text = "/".join(str(part) for part in parts)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def turn_span_id(case_id: UUID, request_id: str) -> str:
    """The span of one agent turn. Derived from the request, so every other row written by
    that request can name its parent without looking anything up."""
    return _span_id("turn", case_id, request_id)


def spans_from_event(row: dict[str, Any], turn_requests: set[tuple[UUID, str]]) -> list[Span]:
    """Spans for one case_events row. `row` carries `entered_at`: when the case entered the
    state this event moves it out of."""
    case_id: UUID = row["case_id"]
    request_id: str | None = row["request_id"]
    trace = trace_id(case_id)
    at: datetime = row["created_at"]
    detail: dict[str, Any] = row["detail"] or {}
    spans: list[Span] = []

    waited_on = HUMAN_WAITS.get(row["from_state"] or "")
    if waited_on and row["to_state"] != row["from_state"] and row.get("entered_at"):
        spans.append(
            Span(
                trace,
                _span_id("wait", row["id"]),
                None,
                "wait",
                f"waiting for {waited_on}",
                row["entered_at"],
                at,
                {
                    "human_wait": True,
                    "waiting_for": waited_on,
                    "seconds": round((at - row["entered_at"]).total_seconds(), 3),
                },
                source=("event", row["id"]),
            )
        )

    if row["kind"] == "agent_turn":
        active = timedelta(milliseconds=detail.get("active_ms") or 0)
        start = at - active
        turn = turn_span_id(case_id, request_id) if request_id else _span_id("event", row["id"])
        spans.append(
            Span(
                trace,
                turn,
                None,
                "agent",
                f"turn {detail.get('turn', '?')}: {detail.get('outcome', 'done')}",
                start,
                at,
                {
                    "request_id": request_id,
                    "graph_version": detail.get("graph_version"),
                    "turn": detail.get("turn"),
                    "outcome": detail.get("outcome"),
                    "interpretation": detail.get("interpretation"),
                    "model_available": detail.get("model_available"),
                    "budget_blocked": detail.get("budget_blocked"),
                    "active_ms": detail.get("active_ms"),
                },
                source=("event", row["id"]),
            )
        )
        for index, call in enumerate(detail.get("tool_calls") or []):
            began = start + timedelta(milliseconds=call.get("at_ms") or 0)
            spans.append(
                Span(
                    trace,
                    _span_id("tool", row["id"], index),
                    turn,
                    "retriever" if call["tool"] == "search_policy" else "tool",
                    call["tool"],
                    began,
                    began + timedelta(milliseconds=call.get("ms") or 0),
                    {
                        "request_id": request_id,
                        "arguments": call.get("arguments"),
                        "error": call.get("error"),
                        **(call.get("result") or {}),
                    },
                    level="ERROR" if call.get("error") else "DEFAULT",
                    status_message=call.get("error"),
                    source=("event", row["id"]),
                )
            )
        return spans

    parent = None
    if request_id and (case_id, request_id) in turn_requests:
        parent = turn_span_id(case_id, request_id)
    spans.append(
        Span(
            trace,
            _span_id("event", row["id"]),
            parent,
            "event",
            row["kind"],
            at,
            at,
            {
                "request_id": request_id,
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "actor_kind": row["actor_kind"],
                "detail": detail,
            },
            source=("event", row["id"]),
        )
    )
    return spans


def span_from_model_attempt(row: dict[str, Any], turn_requests: set[tuple[UUID, str]]) -> Span:
    case_id: UUID = row["case_id"]
    request_id: str | None = row["request_id"]
    at: datetime = row["created_at"]
    price = list_price_usd(
        row["route"], row["model"], row["outcome"], row["input_tokens"], row["output_tokens"]
    )
    parent = None
    if request_id and (case_id, request_id) in turn_requests:
        parent = turn_span_id(case_id, request_id)
    return Span(
        trace_id(case_id),
        _span_id("attempt", row["id"]),
        parent,
        "generation",
        f"{row['purpose']} ({row['tier']})",
        at - timedelta(milliseconds=row["latency_ms"] or 0),
        at,
        {
            "request_id": request_id,
            "turn": row["turn"],
            "purpose": row["purpose"],
            "route": row["route"],
            "model": row["model"],
            "tier": row["tier"],
            "outcome": row["outcome"],
            "input_tokens": row["input_tokens"],
            "output_tokens": row["output_tokens"],
            "latency_ms": row["latency_ms"],
            "list_price_usd": None if price is None else round(price, 8),
            "priced": price is not None,
            "pricing_version": PRICING_VERSION,
        },
        level="DEFAULT" if row["outcome"] == "ok" else "WARNING",
        status_message=row["error"],
        source=("model_attempt", row["id"]),
    )


def span_from_action_attempt(row: dict[str, Any]) -> Span:
    """One call to the payment processor. `row` is an action_attempts row joined to its
    action's case, kind and authorizing request."""
    at: datetime = row["at"]
    failed = row["outcome"] in ("unknown", "rejected")
    return Span(
        trace_id(row["case_id"]),
        _span_id("action_attempt", row["id"]),
        None,
        "tool",
        f"processor.{row['operation']}",
        at,
        at,
        {
            # The request that authorized the action, not the one that happened to run it.
            "request_id": row["request_id"],
            "action_id": str(row["action_id"]),
            "action_kind": row["action_kind"],
            "attempt_no": row["attempt_no"],
            "outcome": row["outcome"],
            **(row["detail"] or {}),
        },
        level="WARNING" if failed else "DEFAULT",
        source=("action_attempt", row["id"]),
    )


def totals(spans: list[Span]) -> dict[str, Any]:
    generations = [s for s in spans if s.kind == "generation"]
    waits: dict[str, float] = {}
    for span in spans:
        if span.kind == "wait":
            who = span.attributes["waiting_for"]
            waits[who] = round(waits.get(who, 0.0) + span.attributes["seconds"], 3)
    priced = [s.attributes["list_price_usd"] for s in generations if s.attributes["priced"]]
    return {
        # Service time only. Waiting for a person is listed on its own below.
        "active_ms": sum(s.attributes.get("active_ms") or 0 for s in spans if s.kind == "agent"),
        "human_wait_seconds": waits,
        "model_attempts": len(generations),
        "model_attempts_not_ok": sum(1 for s in generations if s.attributes["outcome"] != "ok"),
        "input_tokens": sum(s.attributes["input_tokens"] or 0 for s in generations),
        "output_tokens": sum(s.attributes["output_tokens"] or 0 for s in generations),
        "list_price_usd": round(sum(priced), 8),
        "unpriced_attempts": sum(1 for s in generations if not s.attributes["priced"]),
        "pricing_version": PRICING_VERSION,
    }
