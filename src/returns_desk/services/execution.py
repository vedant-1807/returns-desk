"""Authorizing and executing refunds.

Two halves, deliberately separate:

`authorize_and_queue` runs inside the caller's request transaction (shopper or supervisor).
It reserves the units and cents, writes the action with its idempotency key and queues it.
All three commit together or not at all.

`ExecutionService` is the one place that talks to the payment processor. It runs as an admin
job, after the authorizing transaction has committed, and it is safe to run any number of
times: one logical refund keeps one idempotency key for its whole life.
"""

from __future__ import annotations

import logging
import os
import socket
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from returns_desk.clock import Clock
from returns_desk.config import Settings
from returns_desk.db import Conn, Database
from returns_desk.errors import Conflict
from returns_desk.processor.base import (
    PaymentProcessor,
    ProcessorRejected,
    ProcessorTimeout,
    RefundReceipt,
)
from returns_desk.services.common import add_message, format_usd, move_case

log = logging.getLogger(__name__)

TERMINAL = ("succeeded", "failed", "cancelled")


def authorize_and_queue(
    conn: Conn,
    *,
    case: dict[str, Any],
    proposal: dict[str, Any],
    line: dict[str, Any],
    operational_version_id: UUID | None,
    now: datetime,
) -> UUID:
    """Reserve, record and queue one refund in the caller's open transaction.

    The caller must hold the lock on the order line (see load_line_for_update). The balance
    is also guarded by check constraints on order_lines, so a bug here cannot over-refund.
    """
    quantity = proposal["quantity"]
    amount = proposal["amount_cents"]
    try:
        with conn.transaction():
            conn.execute(
                """
                update order_lines
                set reserved_quantity = reserved_quantity + %s,
                    reserved_cents = reserved_cents + %s
                where id = %s
                """,
                (quantity, amount, line["id"]),
            )
    except psycopg.errors.CheckViolation as exc:
        raise Conflict("The requested units are no longer available to refund") from exc

    action_id: UUID = proposal["action_id"]
    conn.execute(
        """
        insert into resolution_actions
          (id, merchant_id, case_id, proposal_id, kind, order_id, order_line_id, quantity,
           amount_cents, provider_payment_ref, idempotency_key, status,
           operational_policy_version_id, created_at, updated_at)
        values (%s, %s, %s, %s, 'refund', %s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
        """,
        (
            action_id,
            case["merchant_id"],
            case["id"],
            proposal["id"],
            line["order_id"],
            line["id"],
            quantity,
            amount,
            line["provider_payment_ref"],
            f"rd-refund-{action_id}",
            operational_version_id,
            now,
            now,
        ),
    )
    conn.execute(
        """
        insert into outbox (merchant_id, action_id, available_at, created_at)
        values (%s, %s, %s, %s)
        """,
        (case["merchant_id"], action_id, now, now),
    )
    return action_id


class ExecutionService:
    def __init__(
        self,
        db: Database,
        processor: PaymentProcessor,
        clock: Clock,
        settings: Settings,
        worker_id: str | None = None,
    ) -> None:
        self._db = db
        self._processor = processor
        self._clock = clock
        self._settings = settings
        self._worker = worker_id or f"{socket.gethostname()}:{os.getpid()}"

    # --- public -----------------------------------------------------------------------

    def run_due(self, limit: int = 20) -> dict[str, int]:
        """Process queued work that is due. Called by POST /internal/reconcile."""
        now = self._clock.now()
        with self._db.service_tx() as conn:
            rows = conn.execute(
                """
                select action_id from outbox
                where done_at is null
                  and available_at <= %(now)s
                  and (lease_until is null or lease_until <= %(now)s)
                order by available_at, id
                limit %(limit)s
                """,
                {"now": now, "limit": limit},
            ).fetchall()
        summary: dict[str, int] = {"claimed": 0}
        for row in rows:
            status = self.process(row["action_id"])
            if status is None:
                continue
            summary["claimed"] += 1
            summary[status] = summary.get(status, 0) + 1
        return summary

    def process(self, action_id: UUID) -> str | None:
        """Make one attempt at one action. Returns the action's status afterwards, or None
        when another worker holds it or it is not due yet."""
        claimed = self._claim(action_id)
        if claimed is None:
            return None
        action, attempt_no = claimed
        attempts: list[tuple[str, str, dict[str, Any]]] = []
        operation = "find_refund" if attempt_no > 1 else "create_refund"
        try:
            receipt: RefundReceipt | None = None
            if attempt_no > 1:
                # An earlier attempt may have reached the processor. Look before sending
                # anything again; this also covers an expired idempotency window.
                receipt = self._processor.find_refund(
                    payment_ref=action["provider_payment_ref"], action_id=str(action["id"])
                )
                attempts.append(
                    ("find_refund", "found" if receipt else "not_found", _receipt_detail(receipt))
                )
            if receipt is None:
                operation = "create_refund"
                receipt = self._processor.create_refund(
                    payment_ref=action["provider_payment_ref"],
                    amount_cents=action["amount_cents"],
                    idempotency_key=action["idempotency_key"],
                    metadata={
                        "action_id": str(action["id"]),
                        "case_id": str(action["case_id"]),
                        "merchant_id": str(action["merchant_id"]),
                    },
                )
                attempts.append(("create_refund", "succeeded", _receipt_detail(receipt)))
        except ProcessorRejected as exc:
            attempts.append((operation, "rejected", {"code": exc.code}))
            return self._settle_failure(action["id"], attempt_no, attempts, exc.code)
        except ProcessorTimeout as exc:
            attempts.append((operation, "unknown", {"error": str(exc)}))
            return self._mark_unknown(action["id"], attempt_no, attempts, str(exc))
        except Exception as exc:  # noqa: BLE001 - any other failure leaves the outcome unknown
            log.exception("unexpected processor error for action %s", action["id"])
            attempts.append((operation, "unknown", {"error": repr(exc)}))
            return self._mark_unknown(action["id"], attempt_no, attempts, repr(exc))
        return self._settle_success(action["id"], attempt_no, attempts, receipt)

    # --- steps ------------------------------------------------------------------------

    def _claim(self, action_id: UUID) -> tuple[dict[str, Any], int] | None:
        """Take the lease and count the attempt, committed before any call goes out. If the
        process dies mid-call, the next worker sees attempt_count > 0 and reconciles first."""
        now = self._clock.now()
        with self._db.service_tx() as conn:
            job = conn.execute(
                """
                update outbox
                set lease_until = %(until)s, lease_owner = %(owner)s, attempts = attempts + 1
                where action_id = %(action)s
                  and done_at is null
                  and available_at <= %(now)s
                  and (lease_until is null or lease_until <= %(now)s)
                returning id
                """,
                {
                    "until": now + timedelta(seconds=self._settings.lease_seconds),
                    "owner": self._worker,
                    "action": action_id,
                    "now": now,
                },
            ).fetchone()
            if job is None:
                return None
            action = conn.execute(
                "select * from resolution_actions where id = %s for update", (action_id,)
            ).fetchone()
            if action["status"] in TERMINAL:
                conn.execute(
                    "update outbox set done_at = %s, lease_until = null where action_id = %s",
                    (now, action_id),
                )
                return None
            attempt_no = action["attempt_count"] + 1
            conn.execute(
                "update resolution_actions set attempt_count = %s, updated_at = %s where id = %s",
                (attempt_no, now, action_id),
            )
            return action, attempt_no

    def _settle_success(
        self,
        action_id: UUID,
        attempt_no: int,
        attempts: list[tuple[str, str, dict[str, Any]]],
        receipt: RefundReceipt,
    ) -> str:
        now = self._clock.now()
        with self._db.service_tx() as conn:
            action = conn.execute(
                "select * from resolution_actions where id = %s for update", (action_id,)
            ).fetchone()
            self._write_attempts(conn, action, attempt_no, attempts, now)
            if action["status"] in TERMINAL:
                # A confirmed terminal result is never overwritten.
                self._finish_job(conn, action_id, now)
                return str(action["status"])
            if (
                receipt.amount_cents != action["amount_cents"]
                or receipt.payment_ref != action["provider_payment_ref"]
            ):
                # The processor's record does not match what was authorized. Do not settle;
                # keep the reservation and leave it for a person to look at.
                return self._hold_unknown(conn, action, now, "receipt_mismatch")

            conn.execute(
                """
                update resolution_actions
                set status = 'succeeded', provider_refund_id = %s, last_error = null,
                    completed_at = %s, updated_at = %s
                where id = %s
                """,
                (receipt.id, now, now, action_id),
            )
            # Reserved becomes refunded. The line's check constraints hold throughout.
            conn.execute(
                """
                update order_lines
                set reserved_quantity = reserved_quantity - %(q)s,
                    reserved_cents = reserved_cents - %(c)s,
                    refunded_quantity = refunded_quantity + %(q)s,
                    refunded_cents = refunded_cents + %(c)s
                where id = %(line)s
                """,
                {
                    "q": action["quantity"],
                    "c": action["amount_cents"],
                    "line": action["order_line_id"],
                },
            )
            conn.execute(
                """
                insert into ledger_entries
                  (merchant_id, action_id, order_id, order_line_id, kind, quantity,
                   amount_cents, provider_refund_id, recorded_at)
                values (%s, %s, %s, %s, 'refund', %s, %s, %s, %s)
                """,
                (
                    action["merchant_id"],
                    action_id,
                    action["order_id"],
                    action["order_line_id"],
                    action["quantity"],
                    action["amount_cents"],
                    receipt.id,
                    now,
                ),
            )
            case = conn.execute(
                "select * from cases where id = %s for update", (action["case_id"],)
            ).fetchone()
            move_case(
                conn,
                case,
                "resolved",
                kind="refund_confirmed",
                actor_kind="system",
                now=now,
                outcome_type="refund",
                resolved_at=now,
                detail={
                    "action_id": str(action_id),
                    "provider_refund_id": receipt.id,
                    "amount_cents": action["amount_cents"],
                },
            )
            add_message(
                conn,
                merchant_id=action["merchant_id"],
                case_id=action["case_id"],
                author_kind="system",
                body=(
                    f"Your refund of {format_usd(action['amount_cents'])} has been issued. "
                    f"Reference: {receipt.id}."
                ),
                now=now,
            )
            self._finish_job(conn, action_id, now)
        return "succeeded"

    def _settle_failure(
        self,
        action_id: UUID,
        attempt_no: int,
        attempts: list[tuple[str, str, dict[str, Any]]],
        code: str,
    ) -> str:
        """The processor verifiably refused. Only now may the reservation be released."""
        now = self._clock.now()
        with self._db.service_tx() as conn:
            action = conn.execute(
                "select * from resolution_actions where id = %s for update", (action_id,)
            ).fetchone()
            self._write_attempts(conn, action, attempt_no, attempts, now)
            if action["status"] in TERMINAL:
                self._finish_job(conn, action_id, now)
                return str(action["status"])
            conn.execute(
                """
                update resolution_actions
                set status = 'failed', last_error = %s, completed_at = %s, updated_at = %s
                where id = %s
                """,
                (code, now, now, action_id),
            )
            conn.execute(
                """
                update order_lines
                set reserved_quantity = reserved_quantity - %s,
                    reserved_cents = reserved_cents - %s
                where id = %s
                """,
                (action["quantity"], action["amount_cents"], action["order_line_id"]),
            )
            case = conn.execute(
                "select * from cases where id = %s for update", (action["case_id"],)
            ).fetchone()
            move_case(
                conn,
                case,
                "declined",
                kind="refund_failed",
                actor_kind="system",
                now=now,
                outcome_type="payment_failed",
                outcome_reason=code,
                detail={"action_id": str(action_id), "code": code},
            )
            add_message(
                conn,
                merchant_id=action["merchant_id"],
                case_id=action["case_id"],
                author_kind="system",
                body="We could not complete this refund. Nothing was charged or refunded.",
                now=now,
            )
            self._finish_job(conn, action_id, now)
        return "failed"

    def _mark_unknown(
        self,
        action_id: UUID,
        attempt_no: int,
        attempts: list[tuple[str, str, dict[str, Any]]],
        error: str,
    ) -> str:
        now = self._clock.now()
        with self._db.service_tx() as conn:
            action = conn.execute(
                "select * from resolution_actions where id = %s for update", (action_id,)
            ).fetchone()
            self._write_attempts(conn, action, attempt_no, attempts, now)
            if action["status"] in TERMINAL:
                self._finish_job(conn, action_id, now)
                return str(action["status"])
            return self._hold_unknown(conn, action, now, error)

    def _hold_unknown(self, conn: Conn, action: dict[str, Any], now: datetime, error: str) -> str:
        """Outcome unknown: keep the reservation, show 'pending verification', retry later."""
        conn.execute(
            """
            update resolution_actions
            set status = 'reconciling', last_error = %s, updated_at = %s
            where id = %s
            """,
            (error, now, action["id"]),
        )
        case = conn.execute(
            "select * from cases where id = %s for update", (action["case_id"],)
        ).fetchone()
        if case["state"] != "reconciling":
            move_case(
                conn,
                case,
                "reconciling",
                kind="payment_outcome_unknown",
                actor_kind="system",
                now=now,
                detail={"action_id": str(action["id"]), "error": error},
            )
        delay = min(
            self._settings.retry_base_seconds * 2 ** max(action["attempt_count"] - 1, 0),
            self._settings.retry_max_seconds,
        )
        conn.execute(
            """
            update outbox
            set available_at = %s, lease_until = null, lease_owner = null
            where action_id = %s
            """,
            (now + timedelta(seconds=delay), action["id"]),
        )
        return "reconciling"

    def _finish_job(self, conn: Conn, action_id: UUID, now: datetime) -> None:
        conn.execute(
            """
            update outbox set done_at = %s, lease_until = null, lease_owner = null
            where action_id = %s and done_at is null
            """,
            (now, action_id),
        )

    def _write_attempts(
        self,
        conn: Conn,
        action: dict[str, Any],
        attempt_no: int,
        attempts: list[tuple[str, str, dict[str, Any]]],
        now: datetime,
    ) -> None:
        for operation, outcome, detail in attempts:
            conn.execute(
                """
                insert into action_attempts
                  (merchant_id, action_id, attempt_no, operation, outcome, detail, worker, at)
                values (%s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    action["merchant_id"],
                    action["id"],
                    attempt_no,
                    operation,
                    outcome,
                    Jsonb(detail),
                    self._worker,
                    now,
                ),
            )


def _receipt_detail(receipt: RefundReceipt | None) -> dict[str, Any]:
    if receipt is None:
        return {}
    return {"refund_id": receipt.id, "amount_cents": receipt.amount_cents}
