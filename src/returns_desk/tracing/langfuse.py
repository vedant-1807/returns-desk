"""Sending traces to Langfuse.

Langfuse takes OpenTelemetry spans over HTTP. This exporter is an admin job: it reads the
audit rows that have not been sent yet, turns them into spans (see spans.py), posts them
as OTLP JSON and records what was sent. A span is sent once. If Langfuse is unreachable,
nothing is recorded and the same rows go out with the next run.

It is off unless RD_LANGFUSE_PUBLIC_KEY and RD_LANGFUSE_SECRET_KEY are set. The trace
itself never depends on it: GET /cases/{id}/trace reads the same rows directly.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx2

from returns_desk.config import Settings
from returns_desk.db import Database
from returns_desk.tracing.spans import Span, trace_id
from returns_desk.tracing.view import (
    ACTION_ATTEMPTS_SQL,
    EVENTS_SQL,
    MODEL_ATTEMPTS_SQL,
    build_spans,
)

log = logging.getLogger(__name__)

SINK = "langfuse"
# Only one exporter runs at a time, across every API process.
_LOCK_KEY = 0x52445452  # "RDTR"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_OBSERVATION_TYPE = {
    "agent": "agent",
    "generation": "generation",
    "tool": "tool",
    "retriever": "retriever",
    "event": "event",
    "wait": "span",
}


def _nanos(moment: datetime) -> str:
    """Nanoseconds since the epoch, as OTLP JSON wants them: a decimal string."""
    return str((moment - _EPOCH) // timedelta(microseconds=1) * 1000)


def _value(value: Any) -> dict[str, Any]:
    """An OTLP AnyValue. Structured values travel as JSON strings."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    return {"stringValue": json.dumps(value, default=str, sort_keys=True)}


def _attributes(pairs: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"key": key, "value": _value(value)} for key, value in pairs.items() if value is not None
    ]


def to_otlp(spans: list[Span], merchants: dict[str, str], *, environment: str) -> dict[str, Any]:
    """OTLP/HTTP JSON for a batch of spans. `merchants` maps a trace ID to its merchant
    slug. Trace-level fields are repeated on every span, as Langfuse asks."""
    out = []
    for span in spans:
        a = span.attributes
        pairs: dict[str, Any] = {
            "langfuse.observation.type": _OBSERVATION_TYPE[span.kind],
            "langfuse.observation.level": span.level,
            "langfuse.observation.status_message": span.status_message,
            "langfuse.trace.name": "returns case",
            "langfuse.trace.metadata.merchant": merchants.get(span.trace_id),
            "langfuse.environment": environment,
        }
        for key, value in a.items():
            if value is not None:
                pairs[f"langfuse.observation.metadata.{key}"] = value
        if span.kind == "generation":
            pairs["langfuse.observation.model.name"] = a["model"]
            pairs["langfuse.observation.usage_details"] = {
                "input": a["input_tokens"] or 0,
                "output": a["output_tokens"] or 0,
            }
            if a["priced"]:
                pairs["langfuse.observation.cost_details"] = {"total": a["list_price_usd"]}
            pairs["langfuse.observation.output"] = {"outcome": a["outcome"]}
        elif span.kind in ("tool", "retriever"):
            pairs["langfuse.observation.input"] = {"arguments": a.get("arguments")}
            kept = {
                k: v
                for k, v in a.items()
                if k not in ("arguments", "request_id", "error") and v is not None
            }
            pairs["langfuse.observation.output"] = kept
        elif span.kind == "agent":
            pairs["langfuse.observation.output"] = {
                "outcome": a.get("outcome"),
                "interpretation": a.get("interpretation"),
            }
        item: dict[str, Any] = {
            "traceId": span.trace_id,
            "spanId": span.span_id,
            "name": span.name,
            "kind": 1,
            "startTimeUnixNano": _nanos(span.start),
            "endTimeUnixNano": _nanos(span.end),
            "attributes": _attributes(pairs),
        }
        if span.parent_id:
            item["parentSpanId"] = span.parent_id
        if span.level == "ERROR":
            item["status"] = {"code": 2, "message": span.status_message or ""}
        out.append(item)
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": _attributes({"service.name": "returns-desk"})},
                "scopeSpans": [{"scope": {"name": "returns_desk.tracing"}, "spans": out}],
            }
        ]
    }


class LangfuseExporter:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        self._db = db
        self._environment = settings.environment
        self._host = settings.langfuse_host.rstrip("/")
        self._project_id = settings.langfuse_project_id or None
        self._project_lock = threading.Lock()
        self._project_checked = bool(self._project_id)
        self._client: httpx2.Client | None = None
        if settings.langfuse_public_key and settings.langfuse_secret_key:
            token = base64.b64encode(
                f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
            ).decode()
            self._client = httpx2.Client(
                base_url=self._host,
                headers={
                    "Authorization": f"Basic {token}",
                    # Real-time ingestion; without it Langfuse may show spans minutes late.
                    "x-langfuse-ingestion-version": "4",
                },
                timeout=10.0,
                transport=transport,
            )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    def trace_url(self, case_id: UUID) -> str | None:
        """The page for this case's trace in Langfuse, when Langfuse is configured."""
        if self._client is None:
            return None
        project = self._project()
        if project is None:
            return None
        return f"{self._host}/project/{project}/traces/{trace_id(case_id)}"

    def _project(self) -> str | None:
        with self._project_lock:
            if not self._project_checked and self._client is not None:
                self._project_checked = True
                try:
                    response = self._client.get("/api/public/projects", timeout=5.0)
                    response.raise_for_status()
                    self._project_id = response.json()["data"][0]["id"]
                except Exception:  # noqa: BLE001 - a link is a convenience, never a failure
                    log.warning("could not look up the Langfuse project; trace links are off")
            return self._project_id

    def export_new(self, limit: int = 300) -> dict[str, int]:
        """Send audit rows that have not been sent yet. Safe to call at any time and from
        several processes: one runs, the others return at once."""
        if self._client is None:
            return {"spans": 0}
        with self._db.service_tx() as conn:
            locked = conn.execute(
                "select pg_try_advisory_xact_lock(%s) as ok", (_LOCK_KEY,)
            ).fetchone()
            if not locked["ok"]:
                return {"spans": 0, "busy": 1}

            def unsent(sql: str, alias: str, source: str, order: str) -> list[dict[str, Any]]:
                return conn.execute(
                    f"""
                    {sql}
                    where not exists (
                      select 1 from trace_exports x
                      where x.sink = %s and x.source = %s and x.source_id = {alias}.id
                    )
                    order by {order} limit %s
                    """,
                    (SINK, source, limit),
                ).fetchall()

            events = unsent(EVENTS_SQL, "e", "event", "e.id")
            model_attempts = unsent(MODEL_ATTEMPTS_SQL, "a", "model_attempt", "a.id")
            action_attempts = unsent(ACTION_ATTEMPTS_SQL, "aa", "action_attempt", "aa.id")
            if not (events or model_attempts or action_attempts):
                return {"spans": 0}

            case_ids = list(
                {row["case_id"] for row in (*events, *model_attempts, *action_attempts)}
            )
            turn_requests = {
                (row["case_id"], row["request_id"])
                for row in conn.execute(
                    """
                    select case_id, request_id from case_events
                    where kind = 'agent_turn' and request_id is not null and case_id = any(%s)
                    """,
                    (case_ids,),
                ).fetchall()
            }
            merchants = {
                trace_id(row["id"]): row["slug"]
                for row in conn.execute(
                    """
                    select c.id, m.slug from cases c join merchants m on m.id = c.merchant_id
                    where c.id = any(%s)
                    """,
                    (case_ids,),
                ).fetchall()
            }
            spans = build_spans(events, model_attempts, action_attempts, turn_requests)
            try:
                response = self._client.post(
                    "/api/public/otel/v1/traces",
                    json=to_otlp(spans, merchants, environment=self._environment),
                )
            except httpx2.HTTPError as exc:
                log.warning("Langfuse export failed, will retry: %r", exc)
                return {"spans": 0, "failed": 1}
            if response.status_code >= 300:
                log.warning(
                    "Langfuse refused the export (%s), will retry: %s",
                    response.status_code,
                    response.text[:200],
                )
                return {"spans": 0, "failed": 1}

            sent = [
                *(("event", row["id"]) for row in events),
                *(("model_attempt", row["id"]) for row in model_attempts),
                *(("action_attempt", row["id"]) for row in action_attempts),
            ]
            with conn.cursor() as cursor:
                cursor.executemany(
                    "insert into trace_exports (sink, source, source_id) values (%s, %s, %s) "
                    "on conflict do nothing",
                    [(SINK, source, source_id) for source, source_id in sent],
                )
        return {"spans": len(spans), "rows": len(sent)}
