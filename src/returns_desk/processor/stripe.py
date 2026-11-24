"""Stripe test mode as the payment processor.

It implements the same small interface as the sandbox, over Stripe's REST API, with no
SDK: create a refund under an idempotency key, and find the refund that belongs to one
application action by the metadata it was created with.

Only test-mode keys are accepted. This project runs on synthetic data and sandbox
payments, so a live key is refused at start-up rather than trusted.

How Stripe's answers map to what the execution service needs:

    2xx                          the refund object, whatever its status
    timeout, network error       ProcessorTimeout: outcome unknown, reconcile first
    429, 5xx, 409                ProcessorTimeout: Stripe may or may not have acted
    401, 403                     ProcessorTimeout: a configuration problem, not a refusal
    idempotency_error            ProcessorTimeout: our key was reused with other values
    other 4xx                    ProcessorRejected: a verified refusal, nothing was paid

A refund Stripe reports as "pending" is passed back as it is. The execution service only
settles on "succeeded" or on a verified failure.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx2

from returns_desk.processor.base import ProcessorRejected, ProcessorTimeout, RefundReceipt

_TEST_KEY_PREFIXES = ("sk_test_", "rk_test_")


def _receipt(obj: dict[str, Any], idempotency_key: str = "") -> RefundReceipt:
    return RefundReceipt(
        id=obj["id"],
        payment_ref=obj.get("payment_intent") or obj.get("charge") or "",
        amount_cents=int(obj["amount"]),
        currency=str(obj.get("currency", "usd")).upper(),
        status=obj.get("status") or "unknown",
        idempotency_key=idempotency_key,
        metadata=obj.get("metadata") or {},
        created_at=datetime.fromtimestamp(int(obj.get("created") or 0), UTC),
    )


class StripeProcessor:
    def __init__(
        self,
        secret_key: str,
        *,
        base_url: str = "https://api.stripe.com",
        timeout_seconds: float = 20.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        if not secret_key.startswith(_TEST_KEY_PREFIXES):
            raise RuntimeError(
                "RD_STRIPE_SECRET_KEY must be a test-mode key (sk_test_...). This project "
                "only ever moves sandbox money."
            )
        self._client = httpx2.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {secret_key}"},
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # --- the provider API the application calls ---------------------------------------

    def create_refund(
        self,
        *,
        payment_ref: str,
        amount_cents: int,
        idempotency_key: str,
        metadata: dict[str, Any],
    ) -> RefundReceipt:
        form = {"payment_intent": payment_ref, "amount": str(amount_cents)}
        form.update({f"metadata[{key}]": str(value) for key, value in metadata.items()})
        obj = self._request(
            "POST", "/v1/refunds", data=form, headers={"Idempotency-Key": idempotency_key}
        )
        return _receipt(obj, idempotency_key)

    def find_refund(self, *, payment_ref: str, action_id: str) -> RefundReceipt | None:
        for obj in self._refunds(payment_ref):
            if (obj.get("metadata") or {}).get("action_id") == action_id:
                return _receipt(obj)
        return None

    # --- used by seeds and tests ------------------------------------------------------

    def create_payment(self, payment_ref: str, account: str, amount_cents: int) -> str:
        """Take a test payment with Stripe's test card and return the PaymentIntent ID,
        which is what a refund is later made against. `payment_ref` is kept as metadata."""
        obj = self._request(
            "POST",
            "/v1/payment_intents",
            data={
                "amount": str(amount_cents),
                "currency": "usd",
                "payment_method": "pm_card_visa",
                "confirm": "true",
                "automatic_payment_methods[enabled]": "true",
                "automatic_payment_methods[allow_redirects]": "never",
                "metadata[seed_ref]": payment_ref,
                "metadata[merchant]": account,
            },
        )
        if obj.get("status") != "succeeded":
            raise RuntimeError(f"Stripe test payment did not succeed: {obj.get('status')}")
        return str(obj["id"])

    def list_refunds(self, payment_ref: str | None = None) -> list[RefundReceipt]:
        if payment_ref is None:
            raise ValueError("Stripe refunds are listed per payment")
        # Oldest first, like the sandbox.
        return [_receipt(obj) for obj in reversed(self._refunds(payment_ref))]

    def reset(self) -> None:
        """Nothing to do: test-mode objects cannot be deleted, and none need to be."""

    def clear_request_log(self) -> None:
        """Nothing to do: Stripe keeps its own request log."""

    # --- internals --------------------------------------------------------------------

    def _refunds(self, payment_ref: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        params: dict[str, str] = {"payment_intent": payment_ref, "limit": "100"}
        while True:
            page = self._request("GET", "/v1/refunds", params=params)
            found.extend(page.get("data") or [])
            if not page.get("has_more") or not page.get("data"):
                return found
            params["starting_after"] = page["data"][-1]["id"]

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx2.TimeoutException as exc:
            raise ProcessorTimeout(f"stripe: no answer to {method} {path}") from exc
        except httpx2.HTTPError as exc:
            raise ProcessorTimeout(f"stripe: connection failed: {exc!r}") from exc

        if response.status_code < 300:
            return response.json()
        try:
            error = response.json().get("error") or {}
        except ValueError:
            error = {}
        kind, code = error.get("type") or "", error.get("code") or ""
        unknown = (
            response.status_code in (401, 403, 409, 429)
            or response.status_code >= 500
            or kind == "idempotency_error"
        )
        if unknown:
            raise ProcessorTimeout(f"stripe: {response.status_code} {kind or code or 'error'}")
        raise ProcessorRejected(
            code or kind or f"http_{response.status_code}", error.get("message") or ""
        )
