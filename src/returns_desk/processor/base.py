from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Protocol


class ProcessorError(Exception):
    """Base class for payment processor failures."""


class ProcessorTimeout(ProcessorError):
    """No answer came back. The refund may or may not exist on the processor's side, so the
    caller must treat the outcome as unknown and reconcile before doing anything else."""


class ProcessorRejected(ProcessorError):
    """The processor refused the refund. This is a verified failure: nothing was paid."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class RefundReceipt:
    id: str
    payment_ref: str
    amount_cents: int
    currency: str
    status: str
    idempotency_key: str
    metadata: dict[str, Any]
    created_at: datetime

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["created_at"] = self.created_at.isoformat()
        return data


class PaymentProcessor(Protocol):
    """What the execution service needs from a payment provider. The sandbox implements it
    now; Stripe test mode implements it in the stretch milestone."""

    def create_refund(
        self,
        *,
        payment_ref: str,
        amount_cents: int,
        idempotency_key: str,
        metadata: dict[str, Any],
    ) -> RefundReceipt:
        """Create a refund, or return the original one when the key was already used."""
        ...

    def find_refund(self, *, payment_ref: str, action_id: str) -> RefundReceipt | None:
        """Look up the refund created for one application action, if it exists. This reads
        the processor's own refund objects, so it works even after the idempotency key's
        replay window has expired."""
        ...
