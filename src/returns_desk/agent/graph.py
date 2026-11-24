"""The agent workflow.

One LangGraph thread per case, checkpointed in Postgres. A shopper turn runs the graph
until it either finishes or pauses with interrupt(): to wait for the shopper's reply, or
to wait for a supervisor's decision. A paused thread survives a restart.

The division of labour never changes:

    model   reads the shopper's words and fills in a form; writes policy answers
    code    checks the form against the real order, plans the next step, picks the words
            for every status and question, and calls the tools
    tools   compute amounts and eligibility on the server and are the only way to act

So the model can be wrong, slow, absent or hostile, and the worst outcome is a clarifying
question or a hand-off to staff.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from psycopg.types.json import Jsonb

from returns_desk.agent import interpret as prompts
from returns_desk.agent import language
from returns_desk.container import Container
from returns_desk.db import Principal
from returns_desk.models.gateway import Attempt, TurnBudget
from returns_desk.services import chat
from returns_desk.services.common import format_usd
from returns_desk.services.views import CUSTOMER_STATUS
from returns_desk.tools.client import ToolFailure, ToolSession

GRAPH_VERSION = "2"


@dataclass
class TurnContext:
    """Run-scoped dependencies. Passed to nodes at run time and never checkpointed."""

    container: Container
    principal: Principal
    tools: ToolSession
    budget: TurnBudget
    request_id: str | None
    attempts: list[Attempt] = field(default_factory=list)
    # Every model answer of this turn in call order, None where no usable answer came
    # back. Kept on the audit trail so a turn can be replayed without a model.
    model_outputs: list[dict[str, Any]] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)


class AgentState(TypedDict, total=False):
    case_id: str
    turn: int
    message: str
    # What has been established so far: intent, line_id, line_options, quantity, reason,
    # explicit (the shopper clearly asked), wants_both, confirmed.
    known: dict[str, Any]
    # The question we are waiting on, if any.
    pending: dict[str, Any] | None
    candidates: list[dict[str, Any]]
    actions: list[dict[str, Any]]
    open_cases: list[dict[str, Any]]
    interpretation: dict[str, Any] | None
    model_available: bool
    quote: dict[str, Any] | None
    step: str
    outcome: str


Node = dict[str, Any]


# --- helpers ----------------------------------------------------------------------------


def _item_label(item: dict[str, Any]) -> str:
    name = item["description"]
    if item.get("variant"):
        name = f"{name} ({item['variant']})"
    return f"{item['quantity']} x {name}" if item.get("quantity", 1) != 1 else name


def _join(options: list[str]) -> str:
    if len(options) <= 1:
        return "".join(options)
    return ", ".join(options[:-1]) + " or " + options[-1]


def _record_attempts(ctx: TurnContext, state: AgentState, attempts: list[Attempt]) -> None:
    """Save every model attempt of this turn, successes and failures alike."""
    if not attempts:
        return
    ctx.attempts.extend(attempts)
    now = ctx.container.clock.now()
    with ctx.container.db.tenant_tx(ctx.principal) as conn:
        for attempt in attempts:
            conn.execute(
                """
                insert into model_attempts
                  (merchant_id, case_id, turn, purpose, route, model, tier, outcome,
                   input_tokens, output_tokens, latency_ms, error, request_id, created_at)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    ctx.principal.merchant_id,
                    state["case_id"],
                    state.get("turn", 1),
                    attempt.purpose,
                    attempt.route,
                    attempt.model,
                    attempt.tier,
                    attempt.outcome,
                    attempt.input_tokens,
                    attempt.output_tokens,
                    attempt.latency_ms,
                    attempt.error,
                    ctx.request_id,
                    now,
                ),
            )


def _log_turn(ctx: TurnContext, state: AgentState, outcome: str, **detail: Any) -> None:
    """One audit event per turn: what the model said, what code decided, which tools ran.
    Tool names and argument keys only; never tool results or internal evidence."""
    now = ctx.container.clock.now()
    with ctx.container.db.tenant_tx(ctx.principal) as conn:
        conn.execute(
            """
            insert into case_events
              (merchant_id, case_id, kind, actor_kind, request_id, detail, created_at)
            values (%s, %s, 'agent_turn', 'system', %s, %s, %s)
            """,
            (
                ctx.principal.merchant_id,
                state["case_id"],
                ctx.request_id,
                Jsonb(
                    {
                        "graph_version": GRAPH_VERSION,
                        "turn": state.get("turn", 1),
                        "outcome": outcome,
                        "interpretation": state.get("interpretation"),
                        "model_available": state.get("model_available", True),
                        "model_outputs": ctx.model_outputs,
                        "strong_tier": ctx.container.models.has_distinct_strong_tier(),
                        "budget_blocked": ctx.budget.blocked_reason,
                        # Time the service spent on this turn. Time spent waiting for
                        # a person is not in here; the trace reports it separately.
                        "active_ms": int((time.monotonic() - ctx.started) * 1000),
                        "model_attempts": [
                            {
                                "purpose": a.purpose,
                                "route": a.route,
                                "model": a.model,
                                "tier": a.tier,
                                "outcome": a.outcome,
                            }
                            for a in ctx.attempts
                        ],
                        "tool_calls": ctx.tools.calls,
                        **detail,
                    }
                ),
                now,
            ),
        )


def _candidate(state: AgentState, line_id: str | None) -> dict[str, Any] | None:
    for candidate in state.get("candidates", []):
        if candidate["line_id"] == line_id:
            return candidate
    return None


# --- nodes ------------------------------------------------------------------------------


def gather(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """Step 3, gather evidence: the order lines this shopper can ask about."""
    ctx = runtime.context
    data = ctx.tools.call("get_order", case_id=state["case_id"])
    candidates: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    open_cases: list[dict[str, Any]] = []
    for order in data["orders"]:
        for line in order["lines"]:
            label = line["description"]
            if line["variant"]:
                label = f"{label}, {line['variant']}"
            candidates.append(
                {
                    "ref": f"L{len(candidates) + 1}",
                    "line_id": line["line_id"],
                    "label": label,
                    "description": line["description"],
                    "variant": line["variant"],
                    "quantity": line["quantity"],
                    "remaining_quantity": line["remaining_quantity"],
                    "order_number": order["order_number"],
                }
            )
        actions.extend(order["actions"])
        open_cases.extend(order["open_cases"])
    return {"candidates": candidates, "actions": actions, "open_cases": open_cases}


def interpret(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """Step 2, interpret: what did the shopper say? The model fills in the form; rules
    handle yes/no replies and veto consent on questions."""
    ctx = runtime.context
    message = state["message"]
    known = dict(state.get("known") or {})
    pending = state.get("pending")
    candidates = state.get("candidates", [])
    refs = [c["ref"] for c in candidates]
    by_ref = {c["ref"]: c for c in candidates}

    # A reply to "do you want me to go ahead?" is decided by a rule, never by a guess.
    if pending and pending["kind"] == "confirm":
        if language.is_affirmative(message):
            known["confirmed"] = True
            return {"known": known, "pending": None, "interpretation": None, "step": "submit"}
        if language.is_negative(message):
            return {"known": known, "pending": None, "interpretation": None, "step": "cancelled"}
        # Something else: treat it as a new message. The old confirmation no longer stands.
        known.pop("confirmed", None)

    request = prompts.interpret_request(message, candidates, pending)
    result = ctx.container.models.complete(
        request, ctx.budget, prompts.validate_interpretation(refs)
    )
    parsed = prompts.clean_interpretation(result.data, refs) if result.ok else None
    ctx.model_outputs.append({"purpose": "interpret", "tier": "low", "data": result.data})

    # What the shopper's own words say about the item, whatever the model thinks.
    labels = [{"ref": c["ref"], "label": c["label"]} for c in candidates]
    evidence, distinct = language.line_evidence(message, labels)

    # The router. Escalate to the stronger model, when there is one and the budget allows,
    # in two cases: the answer settled nothing, or it names a different item than the
    # shopper's words do. Doubt that remains about consent or identity is never settled
    # this way: that always goes back to the shopper as a question.
    conflicting = (
        parsed is not None
        and distinct
        and len(evidence) == 1
        and bool(parsed.lines)
        and list(parsed.lines) != evidence
    )
    unsettled = parsed is not None and ((parsed.intent == "other" and not pending) or conflicting)
    if unsettled and ctx.container.models.has_distinct_strong_tier():
        stronger = ctx.container.models.complete(
            prompts.interpret_request(message, candidates, pending, tier="strong"),
            ctx.budget,
            prompts.validate_interpretation(refs),
        )
        result.attempts.extend(stronger.attempts)
        ctx.model_outputs.append({"purpose": "interpret", "tier": "strong", "data": stronger.data})
        if stronger.ok:
            parsed = prompts.clean_interpretation(stronger.data, refs)
    _record_attempts(ctx, state, result.attempts)

    if parsed is None:
        # No usable model answer. Short replies to our own question can still be read by
        # rule; anything else is handed to staff in the plan step.
        update: Node = {"known": known, "interpretation": None, "model_available": False}
        if pending:
            _apply_reply_rules(known, pending, message, candidates)
            update["known"] = known
        return update

    fresh = not pending or pending["kind"] in ("clarify_intent", "confirm")
    if parsed.intent != "other":
        known["intent"] = parsed.intent
    if fresh:
        known["wants_both"] = parsed.mentions_refund_and_exchange
    if pending and pending["kind"] == "clarify_action":
        lowered = message.lower()
        if "refund" in lowered or "money back" in lowered:
            known["intent"], known["wants_both"] = "refund", False
        elif any(w in lowered for w in ("exchange", "swap", "replace")):
            known["intent"], known["wants_both"] = "exchange", False

    # Which item? The model proposes, the shopper's own words decide. A line is taken as
    # settled only when the text itself singles it out, or there is only one to choose
    # from. A line the model picked without that support is kept as a guess: it can be
    # quoted, but never acted on until the shopper has confirmed it by name.
    model_lines = list(parsed.lines)
    about_the_item = fresh or bool(pending and pending["kind"] == "clarify_line")
    chosen: str | None = None
    verified = False
    options: list[str] = []
    if distinct and len(evidence) == 1:
        if not model_lines or model_lines == evidence:
            chosen, verified = evidence[0], True
        else:
            # The model and the words disagree. Ask.
            options = sorted({*model_lines, *evidence})
    elif distinct:
        options = evidence
    elif len(model_lines) == 1:
        chosen, verified = model_lines[0], len(candidates) == 1
    elif len(model_lines) > 1:
        options = model_lines

    if chosen and (about_the_item or verified):
        known["line_id"] = by_ref[chosen]["line_id"]
        known["line_verified"] = verified
        known.pop("line_options", None)
    elif options and about_the_item:
        known["line_options"] = options
        known.pop("line_id", None)
        known.pop("line_verified", None)

    if parsed.quantity is not None:
        known["quantity"] = parsed.quantity
    # Why? The reason decides between an immediate refund and a review, so it is only
    # taken from the model when the shopper's words back it.
    if parsed.reason is not None and language.reason_supported(message, parsed.reason):
        known["reason"] = parsed.reason
    elif fresh and language.find_reason(message):
        known["reason"] = language.find_reason(message)
    if pending:
        _apply_reply_rules(known, pending, message, candidates)

    # Consent needs the model and the rule to agree. A question is never a request.
    if parsed.explicit_request and not language.is_capability_question(message):
        known["explicit"] = True

    return {
        "known": known,
        "interpretation": parsed.model_dump(),
        "model_available": True,
        "pending": None,
    }


def _apply_reply_rules(
    known: dict[str, Any],
    pending: dict[str, Any],
    message: str,
    candidates: list[dict[str, Any]],
) -> None:
    """Read a short reply to our own question by rule, where the model left a gap."""
    kind = pending["kind"]
    if kind == "clarify_quantity" and "quantity" not in known:
        quantity = language.parse_quantity(message)
        if quantity is not None:
            known["quantity"] = quantity
    elif kind == "clarify_reason" and "reason" not in known:
        # Any explanation we cannot classify is "other", which a person will review.
        known["reason"] = language.find_reason(message) or "other"
    elif kind == "clarify_line" and "line_id" not in known:
        by_ref = {c["ref"]: c for c in candidates}
        options = [by_ref[r] for r in pending.get("options", []) if r in by_ref]
        matched, distinct = language.line_evidence(message, options)
        if distinct and len(matched) == 1:
            known["line_id"] = by_ref[matched[0]]["line_id"]
            known["line_verified"] = True
            known.pop("line_options", None)


def plan(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """Step 4 begins here: code, not the model, decides what happens next."""
    if state.get("step") in ("submit", "cancelled"):
        return {}
    known = dict(state.get("known") or {})
    candidates = state.get("candidates", [])
    step, question = next_step(known, candidates, state.get("model_available", True))
    update: Node = {"step": step, "known": known}
    if question is not None:
        update["pending"] = question
    return update


def next_step(
    known: dict[str, Any], candidates: list[dict[str, Any]], model_available: bool = True
) -> tuple[str, dict[str, Any] | None]:
    """Pure planning: given what is established, what is the next step?

    Returns (step, question). Mutates `known` only to fill in what follows from the order
    itself, such as a quantity of one on a single-unit line.
    """
    intent = known.get("intent")
    if intent is None and not model_available:
        return "handoff", None
    if intent == "status":
        return "status", None
    if intent == "policy_question":
        return "answer_policy", None
    if intent == "exchange" or known.get("wants_both"):
        return "clarify", {
            "kind": "clarify_action",
            "question": "Would you like a refund for this item? "
            "I cannot arrange an exchange in this chat yet.",
        }
    if intent != "refund":
        return "clarify", {
            "kind": "clarify_intent",
            "question": "How can I help? I can refund an item from your order or answer a "
            "question about the return policy.",
        }
    if not candidates:
        return "no_order", None

    by_id = {c["line_id"]: c for c in candidates}
    by_ref = {c["ref"]: c for c in candidates}
    line = by_id.get(known.get("line_id", ""))
    if line is None:
        if len(candidates) == 1:
            line = candidates[0]
            known["line_id"] = line["line_id"]
            known["line_verified"] = True
        else:
            options = [r for r in known.get("line_options", []) if r in by_ref]
            named = bool(options)
            options = options or [c["ref"] for c in candidates[:6]]
            labels = [by_ref[r]["label"] for r in options]
            return "clarify", {
                "kind": "clarify_line",
                "options": options,
                "question": (
                    f"Which one do you mean: {_join(labels)}?"
                    if named
                    else f"Which item is this about: {_join(labels)}?"
                ),
            }

    if known.get("quantity") is None:
        if line["quantity"] == 1 or line["remaining_quantity"] == 1:
            known["quantity"] = 1
        else:
            return "clarify", {
                "kind": "clarify_quantity",
                "question": f"How many of the {line['quantity']} x {line['label']} "
                "would you like to return?",
            }

    if known.get("reason") is None:
        return "clarify", {
            "kind": "clarify_reason",
            "question": "What is the reason for the return? For example: it arrived damaged, "
            "it was the wrong item, or you changed your mind.",
        }
    return "quote", None


def clarify(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """Ask one specific question. Nothing about payment or stock has changed."""
    ctx = runtime.context
    pending = state["pending"] or {}
    now = ctx.container.clock.now()
    with ctx.container.db.tenant_tx(ctx.principal) as conn:
        case = chat.load_case(conn, state["case_id"], lock=True)  # type: ignore[arg-type]
        chat.ask(
            conn,
            case,
            text=pending["question"],
            kind="clarification_requested",
            now=now,
            request_id=ctx.request_id,
            detail={"kind": pending["kind"]},
        )
    _log_turn(ctx, state, "asked", question_kind=pending["kind"])
    return {"outcome": "asked"}


def wait_for_shopper(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """Pause until the shopper replies. The question and everything established so far are
    in the checkpoint, so this survives a restart."""
    reply = interrupt({"waiting_for": "customer", **(state.get("pending") or {})})
    return {
        "message": reply["message"],
        "turn": state.get("turn", 1) + 1,
        "step": "",
        "quote": state.get("quote"),
    }


def answer_policy(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """A policy question: a cited answer and no write to any order."""
    ctx = runtime.context
    question = state["message"]
    found = ctx.tools.call("search_policy", case_id=state["case_id"], question=question, k=5)
    passages = [{**p, "ref": f"P{i + 1}"} for i, p in enumerate(found["passages"])]
    now = ctx.container.clock.now()

    if not found["policy_found"] or not passages:
        # No applicable policy evidence: say so and open it for staff. Never guess.
        with ctx.container.db.tenant_tx(ctx.principal) as conn:
            case = chat.load_case(conn, state["case_id"], lock=True)  # type: ignore[arg-type]
            chat.hand_to_staff(
                conn,
                case,
                text="I could not find a policy section that answers this, so the policy "
                "needs checking. I have passed your question to our team and they will "
                "reply here.",
                reason="policy_needs_verification",
                now=now,
                request_id=ctx.request_id,
            )
        _log_turn(ctx, state, "policy_needs_verification")
        return {"outcome": "handed_to_staff"}

    refs = [p["ref"] for p in passages]
    result = ctx.container.models.complete(
        prompts.answer_request(question, passages), ctx.budget, prompts.validate_answer(refs)
    )
    _record_attempts(ctx, state, result.attempts)
    ctx.model_outputs.append({"purpose": "answer", "tier": "low", "data": result.data})
    by_ref = {p["ref"]: p for p in passages}
    if result.ok:
        text = result.data["answer"].strip()
        cited = [by_ref[r] for r in dict.fromkeys(result.data["cited"]) if r in by_ref]
    else:
        # No model answer within budget: quote the best passage. Grounded by construction.
        top = passages[0]
        text, cited = f"{top['title']}: {top['text']}", [top]
    sources = "; ".join(f"{p['title']} ({p['version_label']})" for p in cited)
    with ctx.container.db.tenant_tx(ctx.principal) as conn:
        case = chat.load_case(conn, state["case_id"], lock=True)  # type: ignore[arg-type]
        chat.answer(
            conn,
            case,
            text=f"{text}\n\nSource: {sources}.",
            reason="policy_question",
            now=now,
            request_id=ctx.request_id,
            detail={
                "cited": [
                    {
                        "passage_id": p["passage_id"],
                        "version_id": p["version_id"],
                        "section_key": p["section_key"],
                    }
                    for p in cited
                ],
                "retrieved": [p["passage_id"] for p in passages],
                "retrieved_sections": [p["section_key"] for p in passages],
                "model_written": result.ok,
            },
        )
    _log_turn(ctx, state, "answered_policy", cited=[p["section_key"] for p in cited])
    return {"outcome": "answered"}


def status(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """A status question: read the existing action and say where it stands."""
    ctx = runtime.context
    known = state.get("known") or {}
    line_id = known.get("line_id")
    related = [
        *[a["case_id"] for a in state.get("actions", []) if not line_id or a["line_id"] == line_id],
        *[
            c["case_id"]
            for c in state.get("open_cases", [])
            if not line_id or c["line_id"] == line_id
        ],
    ]
    related = [case_id for case_id in dict.fromkeys(related) if case_id != state["case_id"]]
    now = ctx.container.clock.now()
    if not related:
        text = "I could not find an earlier request on your recent orders."
        detail: dict[str, Any] = {}
    else:
        # The most recent one. Reuse its reference rather than opening anything new.
        current = ctx.tools.call("get_resolution_status", case_id=related[-1])
        item = _item_label(current["item"]) if current.get("item") else "your item"
        if current.get("receipt"):
            text = (
                f"Your refund of {format_usd(current['receipt']['amount_cents'])} for {item} "
                f"has been issued. Reference: {current['receipt']['refund_reference']}."
            )
        else:
            text = f"About {item}: {current['status_message']}"
        text += f" Request reference: {current['case_id']}."
        detail = {"related_case_id": current["case_id"], "related_state": current["state"]}
    with ctx.container.db.tenant_tx(ctx.principal) as conn:
        case = chat.load_case(conn, state["case_id"], lock=True)  # type: ignore[arg-type]
        chat.answer(
            conn,
            case,
            text=text,
            reason="status",
            now=now,
            request_id=ctx.request_id,
            detail=detail,
        )
    _log_turn(ctx, state, "answered_status", **detail)
    return {"outcome": "answered"}


def quote(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """Step 4, decide: the server computes eligibility and the amount for this exact line
    and quantity. Nothing is reserved and nothing is paid."""
    ctx = runtime.context
    known = state["known"]
    try:
        result = ctx.tools.call(
            "quote_resolution",
            case_id=state["case_id"],
            order_line_id=known["line_id"],
            quantity=known["quantity"],
            reason=known["reason"],
        )
    except ToolFailure as failure:
        return {"step": "handoff", "quote": {"error": failure.code}}

    # Acting without a further question needs two things: the shopper clearly asked, and
    # their own words identify the item. Otherwise say what would happen and ask.
    settled = bool(known.get("explicit")) and bool(known.get("line_verified"))
    if result.get("existing_case_id"):
        step = "existing"
    elif not result["eligible"]:
        step = "submit" if settled else "explain_ineligible"
    elif settled:
        step = "submit"
    else:
        step = "confirm"
    return {"quote": result, "step": step}


def confirm(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """Eligible, but the shopper has not clearly asked for it yet: say what would happen
    and ask. The amount comes from the quote, never from the model."""
    ctx = runtime.context
    q = state["quote"] or {}
    item = _item_label(q["item"])
    policy = q["citations"][0]["version_label"] if q.get("citations") else None
    under = f" under the {policy} policy" if policy else ""
    if q.get("requires_review"):
        question = (
            f"A refund of {format_usd(q['amount_cents'])} for {item} needs a quick check "
            f"by our team first. Shall I send it for review? Reply yes to confirm."
        )
    else:
        question = (
            f"{item} qualifies for a refund of {format_usd(q['amount_cents'])}{under}. "
            "Do you want me to go ahead? Reply yes to confirm."
        )
    now = ctx.container.clock.now()
    with ctx.container.db.tenant_tx(ctx.principal) as conn:
        case = chat.load_case(conn, state["case_id"], lock=True)  # type: ignore[arg-type]
        chat.ask(
            conn,
            case,
            text=question,
            kind="consent_requested",
            now=now,
            request_id=ctx.request_id,
            detail={"proposal_id": q["proposal_id"], "version": q["proposal_version"]},
        )
    _log_turn(ctx, state, "asked", question_kind="confirm", proposal_id=q["proposal_id"])
    return {
        "pending": {"kind": "confirm", "question": question, "proposal_id": q["proposal_id"]},
        "outcome": "asked",
    }


def submit(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """Step 5, act or pause. The tool rechecks everything and is the only way to act."""
    ctx = runtime.context
    q = state.get("quote") or {}
    if not q.get("proposal_id"):
        return {"step": "quote"}
    try:
        result = ctx.tools.call(
            "submit_resolution", case_id=state["case_id"], proposal_id=q["proposal_id"]
        )
    except ToolFailure:
        return {"step": "handoff"}

    outcome = result["outcome"]
    if outcome == "stale_proposal":
        return {"step": "quote"}
    if outcome == "needs_confirmation":
        # The amount moved since the shopper said yes. Ask again with the new amount.
        updated = {
            **q,
            "proposal_id": result["proposal_id"],
            "proposal_version": result["proposal_version"],
            "amount_cents": result["amount_cents"],
        }
        return {"quote": updated, "step": "confirm"}
    if outcome == "pending_review":
        # Logged here, not in the waiting node: a node that calls interrupt() runs again
        # from its first line when the thread resumes.
        _log_turn(ctx, state, "pending_review")
        return {"step": "review_wait", "outcome": "pending_review"}
    return {"step": "respond", "outcome": outcome}


def wait_for_review(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """Pause for a supervisor. The decision arrives through a separate authenticated API
    that this workflow cannot call; that API then resumes the thread."""
    decision = interrupt({"waiting_for": "review"})
    return {"outcome": f"review_{decision.get('decision', 'decided')}", "step": "respond"}


def respond(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """Step 6, verify and respond: make sure the shopper has a status they can revisit.
    The words come from the case's real state, not from the model."""
    ctx = runtime.context
    now = ctx.container.clock.now()
    with ctx.container.db.tenant_tx(ctx.principal) as conn:
        case = chat.load_case(conn, state["case_id"], lock=True)  # type: ignore[arg-type]
        last = conn.execute(
            "select author_kind from case_messages where case_id = %s order by seq desc limit 1",
            (case["id"],),
        ).fetchone()
        if last is None or last["author_kind"] == "shopper":
            chat.say(conn, case, text=CUSTOMER_STATUS[case["state"]][1], now=now)
    _log_turn(ctx, state, state.get("outcome", "responded"))
    return {"pending": None}


def existing(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """These units are already covered by another request. Point at it; start nothing."""
    ctx = runtime.context
    q = state["quote"] or {}
    current = ctx.tools.call("get_resolution_status", case_id=q["existing_case_id"])
    text = (
        f"You already have a request for {_item_label(q['item'])}. "
        f"{current['status_message']} Request reference: {current['case_id']}."
    )
    now = ctx.container.clock.now()
    with ctx.container.db.tenant_tx(ctx.principal) as conn:
        case = chat.load_case(conn, state["case_id"], lock=True)  # type: ignore[arg-type]
        chat.answer(
            conn,
            case,
            text=text,
            reason="existing_resolution",
            now=now,
            request_id=ctx.request_id,
            detail={"related_case_id": current["case_id"]},
        )
    _log_turn(ctx, state, "existing_resolution", related_case_id=current["case_id"])
    return {"outcome": "answered"}


_INELIGIBLE_WORDING = {
    "outside_return_window": "{item} is outside the return window{policy}. "
    "The last day to ask was {last_day}.",
    "quantity_unavailable": "Those units of {item} have already been returned or are part "
    "of another request.",
}


def explain_ineligible(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """The shopper asked whether a return is possible, and it is not. Answer with the
    reason and the policy. Nothing is declined, because nothing was requested."""
    ctx = runtime.context
    q = state["quote"] or {}
    code = (q.get("reason_codes") or ["not_eligible"])[0]
    policy = q["citations"][0]["version_label"] if q.get("citations") else None
    window = (q.get("evidence") or {}).get("window") or {}
    text = _INELIGIBLE_WORDING.get(code, "{item} is not eligible for a return.").format(
        item=_item_label(q["item"]),
        policy=f" in the {policy} policy" if policy else "",
        last_day=window.get("last_day", "earlier"),
    )
    now = ctx.container.clock.now()
    with ctx.container.db.tenant_tx(ctx.principal) as conn:
        case = chat.load_case(conn, state["case_id"], lock=True)  # type: ignore[arg-type]
        chat.answer(
            conn,
            case,
            text=text,
            reason=code,
            now=now,
            request_id=ctx.request_id,
            detail={"proposal_id": q.get("proposal_id")},
        )
    _log_turn(ctx, state, "answered_ineligible", reason=code)
    return {"outcome": "answered"}


def cancelled(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    ctx = runtime.context
    now = ctx.container.clock.now()
    with ctx.container.db.tenant_tx(ctx.principal) as conn:
        case = chat.load_case(conn, state["case_id"], lock=True)  # type: ignore[arg-type]
        chat.answer(
            conn,
            case,
            text="No problem. I have not changed anything.",
            reason="not_confirmed",
            now=now,
            request_id=ctx.request_id,
        )
    _log_turn(ctx, state, "cancelled")
    return {"outcome": "answered", "pending": None}


def no_order(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    ctx = runtime.context
    now = ctx.container.clock.now()
    with ctx.container.db.tenant_tx(ctx.principal) as conn:
        case = chat.load_case(conn, state["case_id"], lock=True)  # type: ignore[arg-type]
        chat.answer(
            conn,
            case,
            text="I could not find an order on your account to refund.",
            reason="no_order",
            now=now,
            request_id=ctx.request_id,
        )
    _log_turn(ctx, state, "no_order")
    return {"outcome": "answered"}


def handoff(state: AgentState, runtime: Runtime[TurnContext]) -> Node:
    """The model gave nothing usable within its budget, or a tool refused. Do not guess:
    tell the shopper a person will pick this up and flag the case."""
    ctx = runtime.context
    now = ctx.container.clock.now()
    with ctx.container.db.tenant_tx(ctx.principal) as conn:
        case = chat.load_case(conn, state["case_id"], lock=True)  # type: ignore[arg-type]
        chat.hand_to_staff(
            conn,
            case,
            text="I am not able to work this out right now, so I have passed your message "
            "to our team. They will reply here.",
            reason="agent_unavailable",
            now=now,
            request_id=ctx.request_id,
        )
    _log_turn(ctx, state, "handed_to_staff")
    return {"outcome": "handed_to_staff"}


# --- wiring -----------------------------------------------------------------------------


def _after_plan(state: AgentState) -> str:
    return state.get("step") or "handoff"


def build_graph() -> StateGraph:
    graph = StateGraph(AgentState, context_schema=TurnContext)
    for name, node in (
        ("gather", gather),
        ("interpret", interpret),
        ("plan", plan),
        ("clarify", clarify),
        ("wait_for_shopper", wait_for_shopper),
        ("answer_policy", answer_policy),
        ("status", status),
        ("quote", quote),
        ("confirm", confirm),
        ("submit", submit),
        ("review_wait", wait_for_review),
        ("respond", respond),
        ("existing", existing),
        ("explain_ineligible", explain_ineligible),
        ("cancelled", cancelled),
        ("no_order", no_order),
        ("handoff", handoff),
    ):
        graph.add_node(name, node)

    graph.add_edge(START, "gather")
    graph.add_edge("gather", "interpret")
    graph.add_edge("interpret", "plan")
    graph.add_conditional_edges(
        "plan",
        _after_plan,
        [
            "answer_policy",
            "status",
            "clarify",
            "quote",
            "submit",
            "cancelled",
            "no_order",
            "handoff",
        ],
    )
    graph.add_edge("clarify", "wait_for_shopper")
    graph.add_edge("confirm", "wait_for_shopper")
    graph.add_edge("wait_for_shopper", "gather")
    graph.add_conditional_edges(
        "quote", _after_plan, ["existing", "explain_ineligible", "submit", "confirm", "handoff"]
    )
    graph.add_conditional_edges(
        "submit", _after_plan, ["respond", "review_wait", "confirm", "quote", "handoff"]
    )
    graph.add_edge("review_wait", "respond")
    for terminal in (
        "answer_policy",
        "status",
        "respond",
        "existing",
        "explain_ineligible",
        "cancelled",
        "no_order",
        "handoff",
    ):
        graph.add_edge(terminal, END)
    return graph
