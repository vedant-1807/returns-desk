"""Record a conversation so it can be run again, and run it again.

A bundle holds what went into a case (the shopper's messages, staff decisions and every
model answer, in order) and what came out (final state, the refund, the tools each turn
called). Replaying it seeds a fresh copy of the same fixture, drives the real service
through the same steps with the recorded model answers fed back in place of a model, and
compares the outcome with the recording.

So a recorded run can be checked again after any change to the code, with no model and no
network, and a difference means the service behaves differently on that conversation.

A bundle contains message text. It is an admin artifact for seeded, synthetic worlds, not
something the API serves.
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from returns_desk.auth import mint_token
from returns_desk.clock import FrozenClock
from returns_desk.config import Settings
from returns_desk.container import build_container
from returns_desk.isolated import isolated_settings
from returns_desk.models.gateway import (
    Attempt,
    ModelRequest,
    ModelResult,
    TurnBudget,
    Validator,
)
from returns_desk.processor.sandbox import SandboxProcessor
from returns_desk.retrieval.embedders import build_embedder
from returns_desk.seeds import seed

REPLAY_DATABASE = "returns_desk_replay"
BUNDLE_VERSION = 1


def record(admin_dsn: str, case_id: UUID, *, fixture: str, seeded_at: datetime, embedder: str):
    """Build the bundle for one chat case. `fixture` and `seeded_at` say which seeded world
    the case ran in; `embedder` is the embedder that world's policy search used."""
    with psycopg.connect(admin_dsn, row_factory=dict_row) as conn:
        case = conn.execute(
            """
            select c.id, c.channel, c.state::text as state, c.outcome_type, c.outcome_reason,
                   m.slug as merchant, c.merchant_id, s.user_id as shopper_user_id
            from cases c
            join merchants m on m.id = c.merchant_id
            join shoppers s on s.id = c.shopper_id
            where c.id = %s
            """,
            (case_id,),
        ).fetchone()
        if case is None:
            raise LookupError(f"no case {case_id}")
        if case["channel"] != "chat":
            raise ValueError("only conversations can be recorded; this case was a typed request")

        opened = conn.execute(
            "select detail from case_events where case_id = %s and kind = 'case_opened'",
            (case_id,),
        ).fetchone()
        messages = conn.execute(
            """
            select body, created_at from case_messages
            where case_id = %s and author_kind = 'shopper' order by seq
            """,
            (case_id,),
        ).fetchall()
        decisions = conn.execute(
            """
            select a.decision, a.note, a.reviewer_user_id, a.decided_at
            from approvals a join proposals p on p.id = a.proposal_id
            where p.case_id = %s order by a.decided_at
            """,
            (case_id,),
        ).fetchall()
        turns = conn.execute(
            "select detail from case_events where case_id = %s and kind = 'agent_turn' order by id",
            (case_id,),
        ).fetchall()
        refunded = conn.execute(
            """
            select coalesce(sum(l.amount_cents), 0)::bigint as cents
            from ledger_entries l join resolution_actions a on a.id = l.action_id
            where a.case_id = %s
            """,
            (case_id,),
        ).fetchone()["cents"]

    steps: list[dict[str, Any]] = [
        {"kind": "message", "at": m["created_at"].isoformat(), "text": m["body"]} for m in messages
    ]
    steps += [
        {
            "kind": "decision",
            "at": d["decided_at"].isoformat(),
            "decision": "approve" if d["decision"] == "approved" else "decline",
            "note": d["note"],
            "reviewer_user_id": str(d["reviewer_user_id"]),
        }
        for d in decisions
    ]
    # In time order. A decision can only follow the message that sent the case to review,
    # so a message wins a tie.
    steps.sort(key=lambda step: (step["at"], step["kind"] != "message"))

    return {
        "bundle_version": BUNDLE_VERSION,
        "recorded_at": datetime.now(UTC).isoformat(),
        "fixture": fixture,
        "seeded_at": seeded_at.isoformat(),
        "embedder": embedder,
        "merchant": case["merchant"],
        "merchant_id": str(case["merchant_id"]),
        "shopper_user_id": str(case["shopper_user_id"]),
        "order_id": (opened["detail"].get("order_id") if opened else None),
        "steps": steps,
        # Every model answer of the run in call order. None means no usable answer came.
        "model_outputs": [
            output for turn in turns for output in (turn["detail"].get("model_outputs") or [])
        ],
        "expected": _outcome(case, refunded, turns),
        "source_case_id": str(case["id"]),
    }


def _outcome(case: dict[str, Any], refunded_cents: int, turns: list[dict[str, Any]]):
    return {
        "state": case["state"],
        "outcome_type": case["outcome_type"],
        "outcome_reason": case["outcome_reason"],
        "refunded_cents": int(refunded_cents),
        "turns": [
            {
                "outcome": turn["detail"].get("outcome"),
                "tools": [call["tool"] for call in turn["detail"].get("tool_calls") or []],
            }
            for turn in turns
        ],
    }


class ReplayGateway:
    """Stands in for the model during a replay: each call gets the next recorded answer."""

    def __init__(self, outputs: list[dict[str, Any]]) -> None:
        self._outputs = deque(outputs)
        # Calls the replay made that the recording has no answer for, or a different one.
        self.mismatches: list[str] = []

    @property
    def unused(self) -> int:
        return len(self._outputs)

    def close(self) -> None:
        return None

    def new_budget(self) -> TurnBudget:
        return TurnBudget(8, 60.0)

    def has_distinct_strong_tier(self) -> bool:
        # The recorded run escalated exactly where its next answer came from the stronger
        # tier. Saying so here makes the replay ask in the same places and nowhere else.
        return bool(self._outputs) and self._outputs[0].get("tier") == "strong"

    def complete(
        self, request: ModelRequest, budget: TurnBudget, validate: Validator
    ) -> ModelResult:
        def attempt(outcome: str, error: str | None = None) -> Attempt:
            return Attempt(
                "replay",
                "replay",
                request.tier,
                request.purpose,
                outcome,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
                error=error,
            )

        if not self._outputs:
            self.mismatches.append(f"a {request.purpose} call the recording does not have")
            return ModelResult(None, [attempt("error", "no recorded answer left")])
        entry = self._outputs.popleft()
        if entry["purpose"] != request.purpose:
            self.mismatches.append(
                f"a {request.purpose} call where the recording has {entry['purpose']}"
            )
            return ModelResult(None, [attempt("error", "recorded answer is for another call")])
        if entry["data"] is None:
            return ModelResult(None, [attempt("timeout")])
        try:
            validate(entry["data"])
        except Exception as exc:  # noqa: BLE001 - the recorded answer no longer validates
            self.mismatches.append(f"the recorded {request.purpose} answer is now rejected: {exc}")
            return ModelResult(None, [attempt("invalid_output", str(exc)[:300])])
        return ModelResult(entry["data"], [attempt("ok")])


def replay(base: Settings, bundle: dict[str, Any]) -> dict[str, Any]:
    """Run a bundle again in an isolated database. Returns the recorded outcome, the
    replayed one, and every difference between them. `matches` is True when there is none."""
    if bundle.get("bundle_version") != BUNDLE_VERSION:
        raise ValueError(f"unsupported bundle version {bundle.get('bundle_version')!r}")
    settings = isolated_settings(base, REPLAY_DATABASE, embedder=bundle["embedder"])
    seeded_at = datetime.fromisoformat(bundle["seeded_at"])
    clock = FrozenClock(seeded_at)
    gateway = ReplayGateway(list(bundle["model_outputs"]))
    embedder = build_embedder(settings)
    container = build_container(settings, clock=clock, models=gateway, embedder=embedder)
    merchant_id = UUID(bundle["merchant_id"])
    case_id: str | None = None
    problems: list[str] = []

    def token(user_id: str, surface: str) -> dict[str, str]:
        minted = mint_token(
            settings,
            user_id=UUID(user_id),
            merchant_id=merchant_id,
            surface=surface,  # type: ignore[arg-type]
            now=datetime.now(UTC),
        )
        return {"Authorization": f"Bearer {minted}"}

    try:
        assert isinstance(container.processor, SandboxProcessor)
        seed(
            settings.admin_dsn,
            container.processor,
            fixture=bundle["fixture"],
            now=seeded_at,
            embedder=embedder,
        )
        from returns_desk.api.app import create_app

        with TestClient(create_app(container)) as client:
            for index, step in enumerate(bundle["steps"]):
                clock.set(datetime.fromisoformat(step["at"]))
                if step["kind"] == "message":
                    shopper = token(bundle["shopper_user_id"], "shopper")
                    if case_id is None:
                        body: dict[str, Any] = {
                            "client_request_id": f"replay-{index}",
                            "message": step["text"],
                        }
                        if bundle.get("order_id"):
                            body["order_id"] = bundle["order_id"]
                        response = client.post("/cases", headers=shopper, json=body)
                    else:
                        response = client.post(
                            f"/cases/{case_id}/messages",
                            headers=shopper,
                            json={"client_request_id": f"replay-{index}", "text": step["text"]},
                        )
                    if response.status_code != 200:
                        problems.append(f"step {index + 1}: HTTP {response.status_code}")
                        break
                    case_id = response.json()["case"]["id"]
                else:
                    staff = token(step["reviewer_user_id"], "staff")
                    pending = client.get("/reviews", headers=staff).json().get("reviews", [])
                    mine = [r for r in pending if r["case_id"] == case_id]
                    if not mine:
                        problems.append(f"step {index + 1}: no review was waiting to be decided")
                        break
                    response = client.post(
                        f"/reviews/{mine[0]['review_id']}/decision",
                        headers=staff,
                        json={"decision": step["decision"], "note": step.get("note", "")},
                    )
                    if response.status_code != 200:
                        problems.append(f"step {index + 1}: HTTP {response.status_code}")
                        break

        replayed = _read_outcome(settings.admin_dsn, case_id) if case_id else None
    finally:
        container.close()

    expected = bundle["expected"]
    problems.extend(gateway.mismatches)
    if gateway.unused:
        problems.append(f"{gateway.unused} recorded model answers were never asked for")
    if replayed is None:
        problems.append("the replay created no case")
    else:
        for key in ("state", "outcome_type", "outcome_reason", "refunded_cents", "turns"):
            if replayed[key] != expected[key]:
                problems.append(f"{key}: recorded {expected[key]!r}, replayed {replayed[key]!r}")
    return {
        "matches": not problems,
        "differences": problems,
        "expected": expected,
        "replayed": replayed,
        "replayed_case_id": case_id,
    }


def _read_outcome(admin_dsn: str, case_id: str) -> dict[str, Any]:
    with psycopg.connect(admin_dsn, row_factory=dict_row) as conn:
        case = conn.execute(
            "select state::text as state, outcome_type, outcome_reason from cases where id = %s",
            (case_id,),
        ).fetchone()
        turns = conn.execute(
            "select detail from case_events where case_id = %s and kind = 'agent_turn' order by id",
            (case_id,),
        ).fetchall()
        refunded = conn.execute(
            """
            select coalesce(sum(l.amount_cents), 0)::bigint as cents
            from ledger_entries l join resolution_actions a on a.id = l.action_id
            where a.case_id = %s
            """,
            (case_id,),
        ).fetchone()["cents"]
    return _outcome(case, refunded, turns)
