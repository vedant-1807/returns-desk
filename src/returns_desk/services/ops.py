"""The operations view: how one merchant's desk is doing.

Read under the caller's row-level security, so the numbers are that merchant's and nothing
else. Figures about live traffic and figures about evaluated cases are kept apart, because
only the eval suite knows whether an outcome was correct.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from returns_desk.db import Conn, Principal
from returns_desk.errors import Forbidden
from returns_desk.models.pricing import PRICING_VERSION, list_price_usd


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def _seconds_since(moment: datetime | None, now: datetime) -> float | None:
    return None if moment is None else round((now - moment).total_seconds(), 1)


def summary(
    conn: Conn, principal: Principal, *, now: datetime, window_days: int = 30
) -> dict[str, Any]:
    if not principal.is_staff:
        raise Forbidden("Only staff can read the operations view")
    since = now - timedelta(days=window_days)

    by_state = {
        row["state"]: row["n"]
        for row in conn.execute(
            "select state::text as state, count(*) as n from cases group by state"
        ).fetchall()
    }
    completed = {
        row["outcome_type"]: row["n"]
        for row in conn.execute(
            """
            select coalesce(outcome_type, 'none') as outcome_type, count(*) as n
            from cases
            where state in ('resolved', 'declined') and updated_at >= %s
            group by 1
            """,
            (since,),
        ).fetchall()
    }
    backlog = conn.execute(
        """
        select count(*) as waiting, min(entered.at) as oldest
        from cases c
        join lateral (
          select e.created_at as at from case_events e
          where e.case_id = c.id and e.to_state = 'awaiting_supervisor'
          order by e.id desc limit 1
        ) entered on true
        where c.state = 'awaiting_supervisor'
        """
    ).fetchone()
    needs_staff = conn.execute(
        """
        select count(*) as n from cases c
        where c.state in ('open', 'awaiting_customer')
          and exists (select 1 from case_events e
                      where e.case_id = c.id and e.kind = 'needs_staff')
        """
    ).fetchone()["n"]
    uncertain = conn.execute(
        """
        select count(*) as reconciling, min(updated_at) as oldest
        from resolution_actions where status = 'reconciling'
        """
    ).fetchone()

    turn_ms = [
        row["ms"]
        for row in conn.execute(
            """
            select (detail ->> 'active_ms')::int as ms from case_events
            where kind = 'agent_turn' and created_at >= %s and detail ? 'active_ms'
            """,
            (since,),
        ).fetchall()
    ]
    attempts = conn.execute(
        """
        select route, model, outcome, input_tokens, output_tokens, latency_ms
        from model_attempts where created_at >= %s
        """,
        (since,),
    ).fetchall()
    cost, unpriced = 0.0, 0
    by_model: dict[str, dict[str, Any]] = {}
    for a in attempts:
        price = list_price_usd(
            a["route"], a["model"], a["outcome"], a["input_tokens"], a["output_tokens"]
        )
        bucket = by_model.setdefault(
            f"{a['route']}/{a['model']}",
            {"attempts": 0, "not_ok": 0, "input_tokens": 0, "output_tokens": 0, "priced": True},
        )
        bucket["attempts"] += 1
        bucket["not_ok"] += int(a["outcome"] != "ok")
        bucket["input_tokens"] += a["input_tokens"] or 0
        bucket["output_tokens"] += a["output_tokens"] or 0
        if price is None:
            unpriced += 1
            bucket["priced"] = False
        else:
            cost += price
    model_ms = [a["latency_ms"] for a in attempts if a["outcome"] == "ok" and a["latency_ms"]]

    automatic = conn.execute(
        """
        select count(distinct c.id) as n
        from cases c
        join proposals p on p.case_id = c.id
        where c.state = 'resolved' and c.outcome_type in ('refund', 'exchange')
          and c.resolved_at >= %s
          -- Paid on the shopper's confirmation alone, with no supervisor involved.
          and p.status = 'submitted' and p.decision = 'auto'
        """,
        (since,),
    ).fetchone()["n"]

    evaluated = conn.execute(
        """
        select suite, label, cases, metrics, by_merchant, recorded_at
        from eval_runs order by recorded_at desc limit 1
        """
    ).fetchone()

    return {
        "merchant": principal.merchant_slug,
        "as_of": now,
        "window_days": window_days,
        "cases": {"by_state": by_state, "completed_in_window": completed},
        "review_backlog": {
            "waiting": backlog["waiting"],
            "oldest_waiting_seconds": _seconds_since(backlog["oldest"], now),
        },
        "needs_staff": needs_staff,
        "uncertain_payments": {
            "reconciling": uncertain["reconciling"],
            "oldest_seconds": _seconds_since(uncertain["oldest"], now),
        },
        "latency": {
            # Service time per agent turn. Time waiting for a person is never in here.
            "turns": len(turn_ms),
            "active_ms_p50": _percentile(turn_ms, 0.50),
            "active_ms_p95": _percentile(turn_ms, 0.95),
            "model_ms_p50": _percentile(model_ms, 0.50),
            "model_ms_p95": _percentile(model_ms, 0.95),
        },
        "model_usage": {
            "attempts": len(attempts),
            "not_ok": sum(1 for a in attempts if a["outcome"] != "ok"),
            "input_tokens": sum(a["input_tokens"] or 0 for a in attempts),
            "output_tokens": sum(a["output_tokens"] or 0 for a in attempts),
            "list_price_usd": round(cost, 6),
            # Shown, never guessed: attempts on a route with no known price.
            "unpriced_attempts": unpriced,
            "cost_complete": unpriced == 0,
            "pricing_version": PRICING_VERSION,
            "by_model": by_model,
        },
        "automatic_resolutions": {
            # Live traffic. Whether each one was correct is only known for evaluated
            # cases, which are reported separately below.
            "count": automatic,
            "list_price_usd_each": round(cost / automatic, 6) if automatic else None,
        },
        "evaluated_cases": _evaluated(evaluated, principal.merchant_slug),
    }


def _evaluated(run: dict[str, Any] | None, merchant: str) -> dict[str, Any] | None:
    """What the latest recorded eval run says about this merchant's cases."""
    if run is None:
        return None
    metrics = run["metrics"]
    mine = (run["by_merchant"] or {}).get(merchant)
    return {
        "suite": run["suite"],
        "label": run["label"],
        "recorded_at": run["recorded_at"],
        "cases": mine["cases"] if mine else 0,
        "correct": mine["correct"] if mine else 0,
        "incorrect_financial_actions": mine["incorrect_financial_actions"] if mine else 0,
        # Across the whole run, and only reported by the suite when no money moved wrongly.
        "cost_per_correct_automatic_resolution_usd": metrics.get("cost", {}).get(
            "per_correct_automatic_resolution_usd"
        ),
        "cost_complete": metrics.get("cost", {}).get("complete"),
        "final_state_accuracy": metrics.get("final_state_accuracy"),
    }
