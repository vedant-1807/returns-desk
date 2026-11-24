"""Signed payment provider events.

The signature scheme is Stripe's, so the same verifier serves Stripe test mode and the
sandbox processor: the header carries a timestamp and an HMAC-SHA256 of
"{timestamp}.{raw body}" under a shared secret.

    Stripe-Signature: t=1700000000,v1=<hex digest>

Verification uses the raw bytes exactly as received, compares digests in constant time,
and rejects a timestamp too far from now, so a captured event cannot be replayed later.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from returns_desk.errors import DomainError

SIGNATURE_HEADER = "stripe-signature"
TOLERANCE_SECONDS = 300


class InvalidSignature(DomainError):
    """The event could not be shown to come from the provider. Nothing is stored."""

    status_code = 400
    code = "invalid_signature"


def sign(payload: bytes, secret: str, timestamp: int) -> str:
    """The header value a provider would send for this payload."""
    digest = hmac.new(secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256)
    return f"t={timestamp},v1={digest.hexdigest()}"


def verify(payload: bytes, header: str | None, secret: str, now: datetime) -> None:
    """Raise InvalidSignature unless the header proves the payload under the secret."""
    if not header or not secret:
        raise InvalidSignature("The event is not signed")
    timestamp: int | None = None
    candidates: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1":
            candidates.append(value)
    if timestamp is None or not candidates:
        raise InvalidSignature("The signature header is malformed")
    if abs(now.timestamp() - timestamp) > TOLERANCE_SECONDS:
        raise InvalidSignature("The event is too old or from the future")
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
        raise InvalidSignature("The signature does not match")


@dataclass(frozen=True)
class RefundEvent:
    """The part of a provider event this service acts on."""

    event_id: str
    event_type: str
    account: str | None
    occurred_at: datetime | None
    refund_id: str | None
    refund_status: str | None
    amount_cents: int | None
    payment_ref: str | None
    action_id: str | None
    failure_reason: str | None
    is_refund: bool


def parse(payload: dict[str, Any]) -> RefundEvent:
    """Read an event in Stripe's shape. Anything that is not about a refund comes back
    with is_refund=False and is stored but never acted on."""
    obj = (payload.get("data") or {}).get("object") or {}
    is_refund = obj.get("object") == "refund"
    metadata = obj.get("metadata") or {}
    created = payload.get("created")
    return RefundEvent(
        event_id=str(payload.get("id") or ""),
        event_type=str(payload.get("type") or ""),
        account=payload.get("account"),
        occurred_at=datetime.fromtimestamp(created, UTC) if isinstance(created, int) else None,
        refund_id=obj.get("id") if is_refund else None,
        refund_status=obj.get("status") if is_refund else None,
        amount_cents=obj.get("amount") if is_refund else None,
        payment_ref=(obj.get("payment_intent") or obj.get("charge")) if is_refund else None,
        action_id=metadata.get("action_id") if is_refund else None,
        failure_reason=obj.get("failure_reason") if is_refund else None,
        is_refund=is_refund,
    )


def encode(payload: dict[str, Any]) -> bytes:
    """The bytes a provider would put on the wire for this event."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
