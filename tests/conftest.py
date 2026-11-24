from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row

from returns_desk.api.app import create_app
from returns_desk.auth import authenticate, mint_token
from returns_desk.clock import FrozenClock
from returns_desk.config import Settings
from returns_desk.container import Container, build_container
from returns_desk.db import Principal
from returns_desk.migrate import migrate
from returns_desk.models import keyword
from returns_desk.models.gateway import ModelRequest, ScriptedGateway
from returns_desk.processor.sandbox import SandboxProcessor
from returns_desk.retrieval.embedders import HashEmbedder
from returns_desk.seeds import Ids, seed
from returns_desk.seeds.demo import STAFF

TEST_DATABASE = "returns_desk_test"

# Every fixture is seeded relative to this instant, so runs are identical.
REFERENCE_NOW = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)

ARTIFACTS = Path(__file__).resolve().parents[1] / "artifacts" / "acceptance"


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Settings pointed at a dedicated test database, created and migrated on first use."""
    base = Settings()
    try:
        with psycopg.connect(base.admin_dsn, autocommit=True, connect_timeout=5) as conn:
            exists = conn.execute(
                "select 1 from pg_database where datname = %s", (TEST_DATABASE,)
            ).fetchone()
            if not exists:
                conn.execute(f'create database "{TEST_DATABASE}"')
    except psycopg.OperationalError as exc:
        pytest.fail(f"Postgres is not reachable. Run `docker compose up -d` first. ({exc})")

    test_settings = base.model_copy(
        update={
            "admin_dsn": make_conninfo(base.admin_dsn, dbname=TEST_DATABASE),
            "app_dsn": make_conninfo(base.app_dsn, dbname=TEST_DATABASE),
            "service_dsn": make_conninfo(base.service_dsn, dbname=TEST_DATABASE),
            "processor_dsn": make_conninfo(base.processor_dsn, dbname=TEST_DATABASE),
            "workflow_dsn": make_conninfo(base.workflow_dsn, dbname=TEST_DATABASE),
            # No model and no network in tests: hashed embeddings and a scripted model.
            "embedder": "hash",
            "model_routes": "keyword",
            "agent_raise_errors": True,
            "environment": "local",
        }
    )
    migrate(test_settings)
    return test_settings


class World:
    """One seeded world for one test: the app, a frozen clock, the sandbox processor, and
    helpers to act as a seeded user and to read persisted state on both sides."""

    def __init__(self, settings: Settings, fixture: str, test_name: str) -> None:
        self.settings = settings
        self.clock = FrozenClock(REFERENCE_NOW)
        self.test_name = test_name
        self._evidence: dict[str, Any] = {"test": test_name, "fixture": fixture, "steps": []}
        # What the "model" says. By default a keyword stand-in reads the message. A test can
        # script exact answers per shopper message, or replace the handler entirely.
        self.script: dict[str, Any] = {}
        self.model = ScriptedGateway(self._model_handler)
        self.embedder = HashEmbedder()
        self.container: Container = self._build()
        seed(
            settings.admin_dsn,
            self.processor,
            fixture=fixture,
            now=self.clock.now(),
            embedder=self.embedder,
        )
        self._open_client()

    def _build(self) -> Container:
        return build_container(
            self.settings, clock=self.clock, models=self.model, embedder=self.embedder
        )

    def _open_client(self) -> None:
        # Entering the client runs the app's lifespan, which the MCP endpoint needs.
        self._client_cm = TestClient(create_app(self.container))
        self.client = self._client_cm.__enter__()

    def _model_handler(self, request: ModelRequest) -> Any:
        key = request.inputs.get("message") or request.inputs.get("question") or ""
        scripted = self.script.get(key, self.script.get(request.purpose))
        if scripted is None:
            return keyword.handle(request)
        return scripted(request) if callable(scripted) else scripted

    @property
    def processor(self) -> SandboxProcessor:
        assert isinstance(self.container.processor, SandboxProcessor)
        return self.container.processor

    def close(self) -> None:
        self._client_cm.__exit__(None, None, None)
        self.container.close()

    def restart(self) -> None:
        """Throw away the running app and start a new one against the same database, as a
        deploy or a crash would. Only what was committed survives."""
        self.close()
        self.container = self._build()
        self._open_client()

    # --- acting as someone --------------------------------------------------------------

    def auth(self, user: str, merchant: str, surface: str | None = None) -> dict[str, str]:
        token = mint_token(
            self.settings,
            user_id=Ids.user(user),
            merchant_id=Ids.merchant(merchant),
            surface=surface or ("staff" if user in STAFF else "shopper"),  # type: ignore[arg-type]
            now=datetime.now(UTC),
        )
        return {"Authorization": f"Bearer {token}"}

    def principal(self, user: str, merchant: str) -> Principal:
        return authenticate(
            self.settings, self.container.db, self.auth(user, merchant)["Authorization"]
        )

    def call(
        self,
        method: str,
        path: str,
        *,
        user: str | None = None,
        merchant: str | None = None,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Response:
        final_headers = dict(headers or {})
        if user and merchant:
            final_headers.update(self.auth(user, merchant))
        response = self.client.request(method, path, headers=final_headers, json=json_body)
        self._evidence["steps"].append(
            {
                "request": f"{method} {path}",
                "as": f"{user}@{merchant}" if user else "anonymous",
                "body": json_body,
                "status": response.status_code,
                "request_id": response.headers.get("x-request-id"),
                "response": _json_or_text(response),
            }
        )
        return response

    def refund_request(
        self,
        merchant: str,
        *,
        number: str = "1001",
        line: str = "blue",
        consent: bool = True,
        client_request_id: str | None = None,
        quantity: int = 1,
    ) -> dict[str, Any]:
        """The reference request, typed: 'The blue one arrived damaged. Refund just that one.'"""
        return {
            "client_request_id": client_request_id or f"req-{uuid4().hex[:12]}",
            "order_id": str(Ids.order(merchant, number)),
            "order_line_id": str(Ids.line(merchant, number, line)),
            "quantity": quantity,
            "action": "refund",
            "reason": "damaged",
            "message": "I ordered two jackets. The blue one arrived damaged. "
            "Please refund just that one.",
            "consent": consent,
        }

    def reconcile(self) -> Response:
        return self.call(
            "POST",
            "/internal/reconcile",
            headers={"Authorization": f"Bearer {self.settings.internal_token}"},
        )

    def say(
        self,
        user: str,
        merchant: str,
        text: str,
        *,
        case_id: str | None = None,
        order: str | None = None,
    ) -> dict[str, Any]:
        """Send one chat message as a shopper: start a conversation, or continue one."""
        request_id = f"msg-{uuid4().hex[:12]}"
        if case_id is None:
            body: dict[str, Any] = {"client_request_id": request_id, "message": text}
            if order:
                body["order_id"] = str(Ids.order(merchant, order))
            response = self.call("POST", "/cases", user=user, merchant=merchant, json_body=body)
        else:
            response = self.call(
                "POST",
                f"/cases/{case_id}/messages",
                user=user,
                merchant=merchant,
                json_body={"client_request_id": request_id, "text": text},
            )
        assert response.status_code == 200, response.text
        return response.json()["case"]

    def mcp(self, user: str | None, merchant: str | None, tool: str, **arguments: Any) -> Any:
        """Call a tool over the real MCP HTTP endpoint with the SDK's own client."""
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        headers = self.auth(user, merchant) if user and merchant else {}
        app = self.client.app

        async def run() -> Any:
            async with (
                httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app),
                    base_url="http://testserver",
                    headers=headers,
                ) as http,
                Client(streamable_http_client("http://testserver/mcp", http_client=http)) as client,
            ):
                return await client.call_tool(tool, arguments)

        result = self.client.portal.call(run)
        self._evidence["steps"].append(
            {
                "request": f"MCP tools/call {tool}",
                "as": f"{user}@{merchant}" if user else "anonymous",
                "body": arguments,
                "is_error": result.is_error,
                "response": result.structured_content
                or [getattr(block, "text", None) for block in result.content],
            }
        )
        return result

    # --- reading persisted state --------------------------------------------------------

    def rows(self, query: str, params: Any = None) -> list[dict[str, Any]]:
        """Read application tables as the admin role, outside row-level security."""
        with psycopg.connect(self.settings.admin_dsn, row_factory=dict_row) as conn:
            return conn.execute(query, params).fetchall()

    def admin(self) -> psycopg.Connection[dict[str, Any]]:
        """An admin connection for arranging a scenario, such as changing a merchant's
        budget or a staff role. Use it as a context manager; it commits on exit."""
        return psycopg.connect(self.settings.admin_dsn, row_factory=dict_row)

    def line(self, merchant: str, number: str, key: str) -> dict[str, Any]:
        return self.rows(
            "select * from order_lines where id = %s", (Ids.line(merchant, number, key),)
        )[0]

    def actions(self, merchant: str, number: str = "1001") -> list[dict[str, Any]]:
        return self.rows(
            "select * from resolution_actions where order_id = %s order by created_at, id",
            (Ids.order(merchant, number),),
        )

    def ledger(self, merchant: str, number: str = "1001") -> list[dict[str, Any]]:
        return self.rows(
            "select * from ledger_entries where order_id = %s order by recorded_at, id",
            (Ids.order(merchant, number),),
        )

    def app_side_snapshot(self) -> dict[str, Any]:
        return {
            "cases": self.rows(
                "select id, merchant_id, state, outcome_type, outcome_reason, quantity, "
                "order_line_id, client_request_id from cases "
                "where client_request_id not like 'seed-%%' order by created_at, id"
            ),
            "proposals": self.rows(
                "select p.id, p.case_id, p.version, p.status, p.decision, p.amount_cents, "
                "p.customer_policy_version_id, p.operational_policy_version_id "
                "from proposals p join cases c on c.id = p.case_id "
                "where c.client_request_id not like 'seed-%%' order by p.created_at, p.version"
            ),
            "approvals": self.rows("select * from approvals order by decided_at"),
            "resolution_actions": self.rows(
                "select a.id, a.case_id, a.status, a.amount_cents, a.quantity, "
                "a.order_line_id, a.idempotency_key, a.provider_refund_id, a.attempt_count "
                "from resolution_actions a join cases c on c.id = a.case_id "
                "where c.client_request_id not like 'seed-%%' order by a.created_at"
            ),
            "action_attempts": self.rows(
                "select action_id, attempt_no, operation, outcome from action_attempts order by id"
            ),
            "ledger_entries": self.rows(
                "select l.action_id, l.order_line_id, l.amount_cents, l.provider_refund_id "
                "from ledger_entries l join resolution_actions a on a.id = l.action_id "
                "join cases c on c.id = a.case_id "
                "where c.client_request_id not like 'seed-%%' order by l.recorded_at"
            ),
            "model_attempts": self.rows(
                "select case_id, turn, purpose, route, model, tier, outcome from model_attempts "
                "order by id"
            ),
            "case_events": self.rows(
                "select e.case_id, e.kind, e.from_state, e.to_state, e.actor_kind, e.request_id "
                "from case_events e order by e.id"
            ),
        }

    def save_evidence(self, **extra: Any) -> None:
        """Write this test's request IDs, resulting rows and processor receipts to disk."""
        self._evidence["rows"] = self.app_side_snapshot()
        self._evidence["processor"] = {
            "refunds": [r.as_dict() for r in self.processor.list_refunds()],
            "requests": self.processor.list_requests(),
        }
        self._evidence.update(extra)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.test_name)
        (ARTIFACTS / f"{name}.json").write_text(
            json.dumps(self._evidence, indent=2, default=str), encoding="utf-8"
        )


def _json_or_text(response: Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


@pytest.fixture
def world(settings: Settings, request: pytest.FixtureRequest) -> Iterator[World]:
    """A freshly seeded world. Pick the fixture with @pytest.mark.seed("return_history");
    the default is the two-jacket reference case."""
    marker = request.node.get_closest_marker("seed")
    fixture = marker.args[0] if marker else "two_jackets"
    world = World(settings, fixture, request.node.name)
    try:
        yield world
        if request.node.get_closest_marker("acceptance"):
            world.save_evidence()
    finally:
        world.close()
