from __future__ import annotations

import hashlib
import secrets
from typing import Any, Literal

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from returns_desk.clock import Clock
from returns_desk.processor.base import ProcessorRejected, ProcessorTimeout, RefundReceipt

FaultKind = Literal["lost_response", "timeout_before", "reject"]


def _receipt(row: dict[str, Any]) -> RefundReceipt:
    return RefundReceipt(
        id=row["id"],
        payment_ref=row["payment_ref"],
        amount_cents=row["amount_cents"],
        currency=row["currency"],
        status=row["status"],
        idempotency_key=row["idempotency_key"],
        metadata=row["metadata"],
        created_at=row["created_at"],
    )


class SandboxProcessor:
    """A fault-injecting stand-in for a payment provider.

    It behaves like the real thing where it matters: refunds are idempotent on a key, a
    payment can never be refunded past its amount, and every commit is its own transaction,
    independent of the application's. Faults are armed ahead of time and consumed in order,
    so a "lost response" is repeatable in CI.
    """

    def __init__(self, dsn: str, clock: Clock) -> None:
        self._clock = clock
        self._pool: ConnectionPool[Any] = ConnectionPool(
            dsn,
            min_size=1,
            max_size=3,
            name="rd-processor",
            kwargs={"row_factory": dict_row, "options": "-c timezone=UTC"},
            open=True,
        )

    def close(self) -> None:
        self._pool.close()

    # --- the provider API the application calls ---------------------------------------

    def create_refund(
        self,
        *,
        payment_ref: str,
        amount_cents: int,
        idempotency_key: str,
        metadata: dict[str, Any],
    ) -> RefundReceipt:
        fault = self._take_fault(payment_ref)

        if fault == "timeout_before":
            self._log("create_refund", payment_ref, idempotency_key, "timeout", fault)
            raise ProcessorTimeout("sandbox: timed out before the refund was recorded")
        if fault == "reject":
            self._log("create_refund", payment_ref, idempotency_key, "rejected", fault)
            raise ProcessorRejected("refund_rejected", "sandbox: refund rejected")

        try:
            receipt, replayed = self._record_refund(
                payment_ref, amount_cents, idempotency_key, metadata
            )
        except ProcessorRejected as exc:
            self._log("create_refund", payment_ref, idempotency_key, f"rejected:{exc.code}", None)
            raise

        outcome = "replayed" if replayed else "created"
        if fault == "lost_response":
            # The refund is committed on this side. Only the answer goes missing.
            self._log("create_refund", payment_ref, idempotency_key, f"{outcome}_then_lost", fault)
            raise ProcessorTimeout("sandbox: refund recorded but the response was lost")

        self._log("create_refund", payment_ref, idempotency_key, outcome, None)
        return receipt

    def find_refund(self, *, payment_ref: str, action_id: str) -> RefundReceipt | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                select * from sandbox_processor.refunds
                where payment_ref = %s and metadata ->> 'action_id' = %s
                """,
                (payment_ref, action_id),
            ).fetchone()
        self._log("find_refund", payment_ref, None, "found" if row else "not_found", None)
        return _receipt(row) if row else None

    # --- sandbox controls used by seeds and tests -------------------------------------

    def create_payment(self, payment_ref: str, account: str, amount_cents: int) -> str:
        """Record an original payment and return the reference refunds are made against.
        Here that is the reference asked for; a real provider issues its own."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                insert into sandbox_processor.payments (payment_ref, account, amount_cents)
                values (%s, %s, %s)
                """,
                (payment_ref, account, amount_cents),
            )
        return payment_ref

    def arm_fault(self, kind: FaultKind, *, payment_ref: str | None = None, times: int = 1) -> None:
        """Make the next `times` create_refund calls fail in the given way. With a
        payment_ref, only calls for that payment are affected."""
        with self._pool.connection() as conn:
            conn.execute(
                """
                insert into sandbox_processor.faults (kind, payment_ref, remaining)
                values (%s, %s, %s)
                """,
                (kind, payment_ref, times),
            )

    def refund_event(
        self,
        refund_id: str,
        *,
        status: str | None = None,
        event_type: str = "refund.updated",
    ) -> dict[str, Any]:
        """The event this provider would send about one of its refunds, in Stripe's shape.

        The same refund, type and status always give the same event ID, so sending it twice
        is a repeated delivery. Pass `status="pending"` for an earlier state of the refund,
        which is what a late, out-of-order delivery carries.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                """
                select r.*, p.account from sandbox_processor.refunds r
                join sandbox_processor.payments p on p.payment_ref = r.payment_ref
                where r.id = %s
                """,
                (refund_id,),
            ).fetchone()
        if row is None:
            raise LookupError(f"no sandbox refund {refund_id}")
        status = status or row["status"]
        digest = hashlib.sha256(f"{refund_id}:{event_type}:{status}".encode()).hexdigest()
        return {
            "id": f"evt_sbx_{digest[:20]}",
            "object": "event",
            "type": event_type,
            # An earlier state of the refund happened earlier.
            "created": int(row["created_at"].timestamp()) + (0 if status == "pending" else 1),
            "account": row["account"],
            "data": {
                "object": {
                    "id": row["id"],
                    "object": "refund",
                    "amount": row["amount_cents"],
                    "currency": row["currency"].lower(),
                    "status": status,
                    "payment_intent": row["payment_ref"],
                    "metadata": row["metadata"],
                }
            },
        }

    def list_refunds(self, payment_ref: str | None = None) -> list[RefundReceipt]:
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                select * from sandbox_processor.refunds
                where %(ref)s::text is null or payment_ref = %(ref)s
                order by created_at, id
                """,
                {"ref": payment_ref},
            ).fetchall()
        return [_receipt(r) for r in rows]

    def list_requests(self, payment_ref: str | None = None) -> list[dict[str, Any]]:
        with self._pool.connection() as conn:
            return conn.execute(
                """
                select operation, payment_ref, idempotency_key, outcome, fault, at
                from sandbox_processor.requests
                where %(ref)s::text is null or payment_ref = %(ref)s
                order by id
                """,
                {"ref": payment_ref},
            ).fetchall()

    def clear_request_log(self) -> None:
        with self._pool.connection() as conn:
            conn.execute("delete from sandbox_processor.requests")

    def reset(self) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                delete from sandbox_processor.refunds;
                delete from sandbox_processor.requests;
                delete from sandbox_processor.faults;
                delete from sandbox_processor.payments;
                """
            )

    # --- internals --------------------------------------------------------------------

    def _take_fault(self, payment_ref: str) -> str | None:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                """
                select id, kind from sandbox_processor.faults
                where remaining > 0 and (payment_ref is null or payment_ref = %s)
                order by id
                limit 1
                for update
                """,
                (payment_ref,),
            ).fetchone()
            if row is None:
                return None
            conn.execute(
                "update sandbox_processor.faults set remaining = remaining - 1 where id = %s",
                (row["id"],),
            )
            return row["kind"]

    def _record_refund(
        self,
        payment_ref: str,
        amount_cents: int,
        idempotency_key: str,
        metadata: dict[str, Any],
    ) -> tuple[RefundReceipt, bool]:
        with self._pool.connection() as conn, conn.transaction():
            # Locking the payment serializes refunds against it, so two calls can never
            # both pass the balance check.
            payment = conn.execute(
                "select * from sandbox_processor.payments where payment_ref = %s for update",
                (payment_ref,),
            ).fetchone()
            if payment is None:
                raise ProcessorRejected("payment_not_found")

            existing = conn.execute(
                "select * from sandbox_processor.refunds where idempotency_key = %s",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["payment_ref"] == payment_ref
                    and existing["amount_cents"] == amount_cents
                )
                if not same:
                    raise ProcessorRejected("idempotency_key_reused")
                return _receipt(existing), True

            refunded = conn.execute(
                """
                select coalesce(sum(amount_cents), 0)::bigint as total
                from sandbox_processor.refunds where payment_ref = %s
                """,
                (payment_ref,),
            ).fetchone()["total"]
            if amount_cents <= 0 or refunded + amount_cents > payment["amount_cents"]:
                raise ProcessorRejected("amount_exceeds_refundable")

            row = conn.execute(
                """
                insert into sandbox_processor.refunds
                  (id, payment_ref, amount_cents, currency, status, idempotency_key,
                   metadata, created_at)
                values (%s, %s, %s, %s, 'succeeded', %s, %s, %s)
                returning *
                """,
                (
                    f"re_sbx_{secrets.token_hex(8)}",
                    payment_ref,
                    amount_cents,
                    payment["currency"],
                    idempotency_key,
                    Jsonb(metadata),
                    self._clock.now(),
                ),
            ).fetchone()
            return _receipt(row), False

    def _log(
        self,
        operation: str,
        payment_ref: str | None,
        idempotency_key: str | None,
        outcome: str,
        fault: str | None,
    ) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                insert into sandbox_processor.requests
                  (operation, payment_ref, idempotency_key, outcome, fault, at)
                values (%s, %s, %s, %s, %s, %s)
                """,
                (operation, payment_ref, idempotency_key, outcome, fault, self._clock.now()),
            )
