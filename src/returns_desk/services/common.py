from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from returns_desk.db import Conn

# States in which a case still lays claim to the units it asked for.
ACTIVE_STATES = ("open", "awaiting_customer", "awaiting_supervisor", "processing", "reconciling")


def format_usd(cents: int) -> str:
    return f"${cents // 100:,}.{cents % 100:02d}"


def record_event(
    conn: Conn,
    *,
    merchant_id: UUID,
    case_id: UUID,
    kind: str,
    actor_kind: str,
    now: datetime,
    from_state: str | None = None,
    to_state: str | None = None,
    actor_user_id: UUID | None = None,
    request_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        insert into case_events
          (merchant_id, case_id, kind, from_state, to_state, actor_kind, actor_user_id,
           request_id, detail, created_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            merchant_id,
            case_id,
            kind,
            from_state,
            to_state,
            actor_kind,
            actor_user_id,
            request_id,
            Jsonb(detail or {}),
            now,
        ),
    )


def add_message(
    conn: Conn,
    *,
    merchant_id: UUID,
    case_id: UUID,
    author_kind: str,
    body: str,
    now: datetime,
    author_user_id: UUID | None = None,
    audience: str = "customer",
    client_request_id: str | None = None,
) -> None:
    conn.execute(
        """
        insert into case_messages
          (merchant_id, case_id, author_kind, author_user_id, audience, body,
           client_request_id, created_at)
        values (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            merchant_id,
            case_id,
            author_kind,
            author_user_id,
            audience,
            body,
            client_request_id,
            now,
        ),
    )


def move_case(
    conn: Conn,
    case: dict[str, Any],
    to_state: str,
    *,
    kind: str,
    actor_kind: str,
    now: datetime,
    actor_user_id: UUID | None = None,
    request_id: str | None = None,
    detail: dict[str, Any] | None = None,
    outcome_type: str | None = None,
    outcome_reason: str | None = None,
    resolved_at: datetime | None = None,
) -> None:
    """Change a case's state and write the audit event in the same transaction."""
    conn.execute(
        """
        update cases
        set state = %s,
            outcome_type = coalesce(%s, outcome_type),
            outcome_reason = coalesce(%s, outcome_reason),
            resolved_at = coalesce(%s, resolved_at),
            updated_at = %s
        where id = %s
        """,
        (to_state, outcome_type, outcome_reason, resolved_at, now, case["id"]),
    )
    record_event(
        conn,
        merchant_id=case["merchant_id"],
        case_id=case["id"],
        kind=kind,
        from_state=case["state"],
        to_state=to_state,
        actor_kind=actor_kind,
        actor_user_id=actor_user_id,
        request_id=request_id,
        detail=detail,
        now=now,
    )
    case["state"] = to_state
