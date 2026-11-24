"""A keyword stand-in for the model.

It fills in the same form a real model does, using fixed word rules. It exists so the whole
agent can run with no model at all: in unit tests, in CI, and as the floor that real models
are compared against in the eval suite. It is not a fallback for production traffic: when a
real model is unavailable the agent asks or hands off, it does not pretend to understand.
"""

from __future__ import annotations

import re
from typing import Any

from returns_desk.agent import language
from returns_desk.config import Settings
from returns_desk.models.gateway import ModelRequest, ScriptedGateway

_STATUS = re.compile(
    r"\b(status|any update|an update|where('s| is) my (refund|money)|"
    r"has my refund|have you (refunded|processed)|when will i (get|receive)|"
    r"did (my|the) refund|still waiting)\b"
)
_EXCHANGE = re.compile(r"\b(exchange|swap|replace(ment)?|different (size|colou?r)|instead)\b")
_REFUND = re.compile(r"\b(refund\w*|money back|return\w*|send (it|them|this) back)\b")
_POLICY = re.compile(
    r"\b(policy|policies|how long|how many days|return window|do you (accept|allow|take)|"
    r"what('s| is| are) (the|your)|rules?)\b"
)


def interpret(inputs: dict[str, Any]) -> dict[str, Any]:
    message: str = inputs["message"]
    lines: list[dict[str, Any]] = inputs.get("lines", [])
    lowered = message.lower()

    matched = language.match_lines(message, [{"ref": x["ref"], "label": x["label"]} for x in lines])
    wants_exchange = bool(_EXCHANGE.search(lowered))
    wants_refund = bool(_REFUND.search(lowered))

    if _STATUS.search(lowered):
        intent = "status"
    elif wants_exchange and not wants_refund:
        intent = "exchange"
    elif wants_refund:
        # A general question about the rules that names no item is a policy question.
        asks = language.is_capability_question(message) or message.strip().endswith("?")
        general = bool(_POLICY.search(lowered)) and not matched and asks
        intent = "policy_question" if general else "refund"
    elif _POLICY.search(lowered):
        intent = "policy_question"
    else:
        intent = "other"

    return {
        "intent": intent,
        "lines": matched if intent in ("refund", "exchange", "status", "other") else [],
        "quantity": language.stated_quantity(message),
        "reason": language.find_reason(message),
        "explicit_request": intent in ("refund", "exchange")
        and language.has_request_marker(message)
        and not language.is_capability_question(message),
        "mentions_refund_and_exchange": wants_exchange and wants_refund,
        # The stand-in does not paraphrase. Code reads the shopper's own words anyway.
        "replacement": None,
    }


def answer(inputs: dict[str, Any]) -> dict[str, Any]:
    """Quote the best passage back. Faithful by construction, if not eloquent."""
    passages: list[dict[str, Any]] = inputs.get("passages", [])
    if not passages:
        return {"answer": "", "cited": []}
    top = passages[0]
    return {"answer": f"{top['title']}: {top['text']}", "cited": [top["ref"]]}


def handle(request: ModelRequest) -> dict[str, Any]:
    if request.purpose == "interpret":
        return interpret(request.inputs)
    return answer(request.inputs)


def keyword_gateway(settings: Settings | None = None) -> ScriptedGateway:
    return ScriptedGateway(handle, name="keyword")
