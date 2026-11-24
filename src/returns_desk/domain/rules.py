"""The decision rules, as pure functions.

Nothing in this module touches the database, the clock or a model. Give it the facts and the
two policy versions and it returns the same answer every time. That is what lets the same
engine produce ground truth for the eval suite later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field


class CustomerTermsRules(BaseModel):
    """Structured fields of a customer terms version (what the shopper agreed to)."""

    model_config = ConfigDict(extra="forbid")

    return_window_days: int = Field(ge=0)
    # Reasons a merchant settles straight away, with no return shipment.
    immediate_refund_reasons: list[str] = Field(default_factory=list)
    window_section: str = "returns.window"
    reason_sections: dict[str, str] = Field(default_factory=dict)


class OperationalRulesBody(BaseModel):
    """Structured fields of an operational rules version (internal, never shown to shoppers)."""

    model_config = ConfigDict(extra="forbid")

    auto_approve_limit_cents: int = Field(ge=0)
    return_history_threshold: int = Field(ge=1)
    return_history_window_days: int = Field(ge=1)


@dataclass(frozen=True)
class CustomerTerms:
    version_id: UUID
    version_label: str
    rules: CustomerTermsRules


@dataclass(frozen=True)
class OperationalRules:
    version_id: UUID
    version_label: str
    rules: OperationalRulesBody


@dataclass(frozen=True)
class LineFacts:
    quantity: int
    unit_paid_cents: int
    reserved_quantity: int
    refunded_quantity: int
    exchanged_quantity: int = 0

    @property
    def remaining_quantity(self) -> int:
        return (
            self.quantity
            - self.reserved_quantity
            - self.refunded_quantity
            - self.exchanged_quantity
        )


@dataclass(frozen=True)
class RequestFacts:
    quantity: int
    reason: str
    requested_at: datetime
    delivered_at: datetime | None
    merchant_timezone: str
    # Distinct completed returns by this shopper at this merchant in the look-back window,
    # not counting the current case.
    prior_completed_returns: int
    # A staff member asked for a supervisor to look at this case.
    staff_escalated: bool = False
    # "refund" or "exchange". An exchange also needs the replacement to exist and be in
    # stock: `replacement_available` is the stock count of the wanted variant, or None
    # when the product has no such variant.
    action: str = "refund"
    replacement_available: int | None = None


class Decision(StrEnum):
    AUTO = "auto"
    REVIEW = "review"
    INELIGIBLE = "ineligible"


class Reason(StrEnum):
    # Hard constraints. No approval can override these.
    INVALID_QUANTITY = "invalid_quantity"
    QUANTITY_UNAVAILABLE = "quantity_unavailable"
    OUTSIDE_RETURN_WINDOW = "outside_return_window"
    # Exchanges only. Out of stock is not the shopper's doing: the service offers another
    # choice instead of closing the request, but it can never be approved away.
    REPLACEMENT_UNKNOWN = "replacement_unknown"
    REPLACEMENT_UNAVAILABLE = "replacement_unavailable"
    # Review signals. A supervisor decides.
    ABOVE_AUTO_LIMIT = "above_auto_limit"
    RETURN_HISTORY_THRESHOLD = "return_history_threshold"
    REASON_REQUIRES_REVIEW = "reason_requires_review"
    DELIVERY_UNCONFIRMED = "delivery_unconfirmed"
    POLICY_NEEDS_VERIFICATION = "policy_needs_verification"
    STAFF_ESCALATION = "staff_escalation"


@dataclass(frozen=True)
class WindowCheck:
    delivered_on: date
    last_day: date
    requested_on: date
    window_days: int

    @property
    def within(self) -> bool:
        return self.requested_on <= self.last_day


@dataclass(frozen=True)
class Evaluation:
    decision: Decision
    amount_cents: int
    hard_reasons: tuple[str, ...] = ()
    review_reasons: tuple[str, ...] = ()
    window: WindowCheck | None = None
    cited_sections: tuple[str, ...] = ()
    # Customer-safe facts behind the decision.
    evidence: dict[str, Any] = field(default_factory=dict)
    # Facts only a supervisor may see (limit, return history).
    internal_evidence: dict[str, Any] = field(default_factory=dict)


def return_window(
    delivered_at: datetime, requested_at: datetime, window_days: int, timezone: str
) -> WindowCheck:
    """Return windows run on the merchant's local calendar and include the last day.

    Delivery day is day 0. With a 30-day window, a request made at any time on local day 30
    is inside the window and day 31 is not.
    """
    tz = ZoneInfo(timezone)
    delivered_on = delivered_at.astimezone(tz).date()
    return WindowCheck(
        delivered_on=delivered_on,
        last_day=delivered_on + timedelta(days=window_days),
        requested_on=requested_at.astimezone(tz).date(),
        window_days=window_days,
    )


def evaluate(
    line: LineFacts,
    request: RequestFacts,
    terms: CustomerTerms | None,
    operational: OperationalRules | None,
) -> Evaluation:
    """Decide one request, a refund or an exchange, for one order line and quantity.

    An exchange is held to the same window, reasons and approval limit as a refund of the
    same units: `amount_cents` is always the paid value of the units in question, which is
    what the limit is compared with. Whether money moves is decided by the action, not by
    this number.
    """
    hard: list[str] = []
    review: list[str] = []
    evidence: dict[str, Any] = {}
    internal: dict[str, Any] = {}
    cited: list[str] = []
    window: WindowCheck | None = None

    if request.quantity < 1:
        return Evaluation(Decision.INELIGIBLE, 0, hard_reasons=(Reason.INVALID_QUANTITY,))

    # The refund is the selected units' own paid value, after discounts. Other lines and the
    # order total play no part.
    amount_cents = line.unit_paid_cents * request.quantity
    evidence["unit_paid_cents"] = line.unit_paid_cents
    evidence["quantity"] = request.quantity

    if request.quantity > line.remaining_quantity:
        hard.append(Reason.QUANTITY_UNAVAILABLE)
        evidence["remaining_quantity"] = line.remaining_quantity

    if request.action == "exchange":
        if request.replacement_available is None:
            hard.append(Reason.REPLACEMENT_UNKNOWN)
        elif request.replacement_available < request.quantity:
            hard.append(Reason.REPLACEMENT_UNAVAILABLE)
            evidence["replacement_available"] = request.replacement_available

    if terms is None:
        # No single applicable version: do not guess, send it to staff.
        review.append(Reason.POLICY_NEEDS_VERIFICATION)
    else:
        evidence["customer_policy_version"] = terms.version_label
        if request.delivered_at is None:
            review.append(Reason.DELIVERY_UNCONFIRMED)
        else:
            window = return_window(
                request.delivered_at,
                request.requested_at,
                terms.rules.return_window_days,
                request.merchant_timezone,
            )
            cited.append(terms.rules.window_section)
            evidence["window"] = {
                "delivered_on": window.delivered_on.isoformat(),
                "last_day": window.last_day.isoformat(),
                "requested_on": window.requested_on.isoformat(),
                "window_days": window.window_days,
                "within": window.within,
            }
            if not window.within:
                hard.append(Reason.OUTSIDE_RETURN_WINDOW)

        if request.reason in terms.rules.immediate_refund_reasons:
            section = terms.rules.reason_sections.get(request.reason)
            if section:
                cited.append(section)
        else:
            review.append(Reason.REASON_REQUIRES_REVIEW)

    if operational is None:
        if Reason.POLICY_NEEDS_VERIFICATION not in review:
            review.append(Reason.POLICY_NEEDS_VERIFICATION)
    else:
        rules = operational.rules
        internal["operational_policy_version"] = operational.version_label
        internal["auto_approve_limit_cents"] = rules.auto_approve_limit_cents
        internal["prior_completed_returns"] = request.prior_completed_returns
        internal["return_history_threshold"] = rules.return_history_threshold
        internal["return_history_window_days"] = rules.return_history_window_days
        # Exactly the limit is still automatic.
        if amount_cents > rules.auto_approve_limit_cents:
            review.append(Reason.ABOVE_AUTO_LIMIT)
        if request.prior_completed_returns >= rules.return_history_threshold:
            review.append(Reason.RETURN_HISTORY_THRESHOLD)

    if request.staff_escalated:
        review.append(Reason.STAFF_ESCALATION)

    if hard:
        decision = Decision.INELIGIBLE
    elif review:
        decision = Decision.REVIEW
    else:
        decision = Decision.AUTO

    return Evaluation(
        decision=decision,
        amount_cents=amount_cents,
        hard_reasons=tuple(str(r) for r in hard),
        review_reasons=tuple(str(r) for r in review),
        window=window,
        cited_sections=tuple(cited),
        evidence=evidence,
        internal_evidence=internal,
    )
