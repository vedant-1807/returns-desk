from __future__ import annotations

import hmac
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from returns_desk.auth import authenticate, mint_token
from returns_desk.container import Container, build_container
from returns_desk.db import Principal
from returns_desk.errors import (
    DomainError,
    Forbidden,
    InvalidRequest,
    NotAuthenticated,
    NotFound,
)
from returns_desk.retrieval.index import index_policy_versions
from returns_desk.services import cases, chat, ops, policies, reviews, views
from returns_desk.services.common import add_message
from returns_desk.services.views import CUSTOMER_STATUS
from returns_desk.tools.server import build_tool_server, http_app
from returns_desk.tracing import view as trace_view
from returns_desk.tracing.spans import trace_id

__all__ = ["Container", "build_container", "create_app"]

log = logging.getLogger(__name__)

_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,100}$")


class PolicySectionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    section_key: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    audience: str = Field(pattern="^(customer|associate|supervisor)$")
    body: str = Field(min_length=1, max_length=8000)


class PublishVersionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version_label: str = Field(min_length=1, max_length=100)
    effective_from: datetime
    rules: dict[str, Any]
    sections: list[PolicySectionInput] = Field(default_factory=list)


class DevSessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    merchant: str


def get_container(request: Request) -> Container:
    return request.app.state.container


# These aliases live at module level because the annotations in this file are strings
# (from __future__ import annotations) and FastAPI resolves them against module globals.
Deps = Annotated[Container, Depends(get_container)]


def get_principal(deps: Deps, authorization: Annotated[str | None, Header()] = None) -> Principal:
    return authenticate(deps.settings, deps.db, authorization)


Caller = Annotated[Principal, Depends(get_principal)]


class _McpPathAlias:
    """POST /mcp and POST /mcp/ are the same endpoint."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"] == "/mcp":
            scope = {**scope, "path": "/mcp/", "raw_path": b"/mcp/"}
        await self.app(scope, receive, send)


def create_app(container: Container | None = None) -> FastAPI:
    owns_container = container is None
    container = container or build_container()
    settings = container.settings
    # The HTTP face of the tool server: no bound principal, so every call authenticates.
    mcp_server = build_tool_server(container)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with mcp_server.session_manager.run():
            yield
        if owns_container:
            app.state.container.close()

    app = FastAPI(title="Returns Desk", version="0.2.0", lifespan=lifespan)
    app.state.container = container
    allowed_hosts = [h.strip() for h in settings.mcp_allowed_hosts.split(",") if h.strip()]
    app.mount("/mcp", http_app(mcp_server, allowed_hosts))
    app.add_middleware(_McpPathAlias)
    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_methods=["GET", "POST"],
            allow_headers=["authorization", "content-type", "x-request-id"],
            expose_headers=["x-request-id"],
        )

    # --- plumbing ---------------------------------------------------------------------

    @app.middleware("http")
    async def request_id_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """One correlation ID per request. It is saved on every audit event the request
        writes and echoed back, so a response can be traced to its rows."""
        incoming = request.headers.get("x-request-id", "")
        request.state.request_id = (
            incoming if _REQUEST_ID.match(incoming) else f"req_{uuid.uuid4().hex}"
        )
        response = await call_next(request)
        response.headers["x-request-id"] = request.state.request_id
        return response

    @app.exception_handler(DomainError)
    async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
        error: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.details:
            error["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content={"error": error})

    @app.exception_handler(ValidationError)
    async def validation_error_handler(request: Request, exc: ValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The request body is not valid",
                    "details": {"errors": errors},
                }
            },
        )

    def execute_now(deps: Container, action_id: UUID | None) -> None:
        """Try a freshly authorized action once, straight away, so the usual case answers in
        one round trip. The authorization is already committed, so if this attempt fails for
        any reason the reconcile job picks the action up from the outbox."""
        if action_id is None:
            return
        try:
            deps.execution.process(action_id)
        except Exception:  # noqa: BLE001
            log.exception("inline execution failed for action %s; left to reconcile", action_id)

    def render_case(deps: Container, principal: Principal, case_id: UUID) -> dict[str, Any]:
        with deps.db.tenant_tx(principal) as conn:
            view = views.case_view(conn, principal, case_id)
        if view is None:
            raise NotFound()
        if principal.is_staff:
            # Where staff can follow the run: the trace endpoint, and Langfuse when set up.
            view["trace"] = {
                "trace_id": trace_id(case_id),
                "path": f"/cases/{case_id}/trace",
                "langfuse_url": deps.tracer.trace_url(case_id),
            }
        return view

    def export_traces(deps: Container) -> None:
        """Send new spans to Langfuse after the response has gone out. Tracing must never
        fail or slow a request, so every error ends here."""
        try:
            deps.tracer.export_new()
        except Exception:  # noqa: BLE001
            log.exception("trace export failed; the rows stay queued for the next run")

    def after_response(deps: Container, background: BackgroundTasks) -> None:
        if deps.tracer.enabled:
            background.add_task(export_traces, deps)

    def run_agent(
        deps: Container, principal: Principal, case_id: UUID, message: str, request_id: str
    ) -> None:
        """Run one shopper turn of the workflow. If the workflow itself breaks, the shopper
        still gets an honest answer and the case is flagged for a person."""
        try:
            deps.agent.shopper_turn(principal, case_id, message, request_id)
        except Exception:
            if deps.settings.agent_raise_errors:
                raise
            log.exception("agent turn failed for case %s", case_id)
            with deps.db.tenant_tx(principal) as conn:
                case = chat.load_case(conn, case_id, lock=True)
                chat.hand_to_staff(
                    conn,
                    case,
                    text="Something went wrong on our side, so I have passed your message to "
                    "our team. They will reply here.",
                    reason="agent_error",
                    now=deps.clock.now(),
                    request_id=request_id,
                )

    def chat_envelope(
        deps: Container, principal: Principal, case_id: UUID, replayed: bool
    ) -> dict[str, Any]:
        view = render_case(deps, principal, case_id)
        outcome = view.get("outcome") or {}
        return {
            "case": view,
            "replayed": replayed,
            "existing_resolution": outcome.get("reason") == "existing_resolution",
        }

    # --- shopper and staff: cases -----------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/me")
    def me(principal: Caller) -> dict[str, Any]:
        return {
            "user_id": principal.user_id,
            "merchant": principal.merchant_slug,
            "role": principal.role,
        }

    @app.get("/orders")
    def list_orders(deps: Deps, principal: Caller) -> dict[str, Any]:
        with deps.db.tenant_tx(principal) as conn:
            return {"orders": views.list_orders(conn)}

    @app.post("/cases")
    def open_case(
        deps: Deps,
        principal: Caller,
        request: Request,
        background: BackgroundTasks,
        body: Annotated[dict[str, Any], Body()],
    ) -> dict[str, Any]:
        """Start a request. Two shapes are accepted:

        * a conversation: {client_request_id, message, order_id?}. The agent works out what
          is being asked.
        * one typed request: {client_request_id, order_id, order_line_id, reason, ...}.
        """
        request_id = request.state.request_id
        after_response(deps, background)
        if "order_line_id" in body:
            typed = cases.OpenCaseInput.model_validate(body)
            result = cases.open_case(deps.db, deps.clock, principal, typed, request_id)
            execute_now(deps, result.action_id)
            return {
                "case": render_case(deps, principal, result.case_id),
                "replayed": result.replayed,
                "existing_resolution": result.existing_resolution,
            }

        data = chat.ChatOpenInput.model_validate(body)
        with deps.db.tenant_tx(principal) as conn:
            case, replayed = chat.open_chat_case(
                conn, principal, data, now=deps.clock.now(), request_id=request_id
            )
        if not replayed:
            run_agent(deps, principal, case["id"], data.message, request_id)
        return chat_envelope(deps, principal, case["id"], replayed)

    @app.get("/cases")
    def list_cases(deps: Deps, principal: Caller) -> dict[str, Any]:
        with deps.db.tenant_tx(principal) as conn:
            return {"cases": views.list_cases(conn, principal)}

    @app.get("/cases/{case_id}")
    def get_case(deps: Deps, principal: Caller, case_id: UUID) -> dict[str, Any]:
        return {"case": render_case(deps, principal, case_id)}

    @app.get("/cases/{case_id}/trace")
    def get_trace(deps: Deps, principal: Caller, case_id: UUID) -> dict[str, Any]:
        """The run behind a case, for staff: every request, model attempt, tool call,
        state change and processor call, as IDs and outcome codes. Built from the audit
        trail under the caller's own access, so it shows what their role may see today."""
        with deps.db.tenant_tx(principal) as conn:
            trace = trace_view.case_trace(conn, principal, case_id)
        if trace is None:
            raise NotFound()
        trace["langfuse_url"] = deps.tracer.trace_url(case_id)
        return {"trace": trace}

    @app.post("/cases/{case_id}/messages")
    def post_message(
        deps: Deps,
        principal: Caller,
        case_id: UUID,
        body: cases.MessageInput,
        request: Request,
        background: BackgroundTasks,
    ) -> dict[str, Any]:
        request_id = request.state.request_id
        after_response(deps, background)
        with deps.db.tenant_tx(principal) as conn:
            found = conn.execute("select channel from cases where id = %s", (case_id,)).fetchone()
        if found is None:
            raise NotFound()
        if found["channel"] != "chat":
            result = cases.post_message(deps.db, deps.clock, principal, case_id, body, request_id)
            execute_now(deps, result.action_id)
            return {
                "case": render_case(deps, principal, result.case_id),
                "replayed": result.replayed,
            }

        text = body.text or ("Yes." if body.consent else "")
        if not text:
            raise InvalidRequest("A message needs text")
        now = deps.clock.now()
        with deps.db.tenant_tx(principal) as conn:
            case = chat.load_case(conn, case_id, lock=True)
            is_owner = principal.role == "shopper" and case["shopper_id"] == principal.shopper_id
            if is_owner:
                fresh = chat.record_reply(
                    conn,
                    principal,
                    case,
                    text=text,
                    client_request_id=body.client_request_id,
                    now=now,
                    request_id=request_id,
                )
            else:
                # Staff can write on a case. That never drives the shopper's workflow.
                seen = conn.execute(
                    "select 1 from case_messages where case_id = %s and client_request_id = %s",
                    (case_id, body.client_request_id),
                ).fetchone()
                fresh = seen is None
                if fresh:
                    add_message(
                        conn,
                        merchant_id=case["merchant_id"],
                        case_id=case_id,
                        author_kind=principal.role,
                        author_user_id=principal.user_id,
                        body=text,
                        client_request_id=body.client_request_id,
                        now=now,
                    )
            state = case["state"]
        if fresh and is_owner:
            if state == "open":
                run_agent(deps, principal, case_id, text, request_id)
            else:
                # In review, in payment or finished: answer from the case's real status.
                with deps.db.tenant_tx(principal) as conn:
                    case = chat.load_case(conn, case_id, lock=True)
                    chat.say(conn, case, text=CUSTOMER_STATUS[state][1], now=deps.clock.now())
        return chat_envelope(deps, principal, case_id, not fresh)

    # --- supervisors: review ----------------------------------------------------------

    @app.get("/reviews")
    def list_reviews(deps: Deps, principal: Caller) -> dict[str, Any]:
        return {"reviews": reviews.list_reviews(deps.db, principal)}

    @app.post("/reviews/{review_id}/decision")
    def decide_review(
        deps: Deps,
        principal: Caller,
        review_id: UUID,
        body: reviews.DecisionInput,
        request: Request,
        background: BackgroundTasks,
    ) -> dict[str, Any]:
        request_id = request.state.request_id
        after_response(deps, background)
        result = reviews.decide(deps.db, deps.clock, principal, review_id, body, request_id)
        execute_now(deps, result.action_id)
        case_id = result.body["case_id"]
        if not result.body["already_decided"]:
            # The decision is committed. Now let the paused conversation continue. If that
            # fails, the decision and any payment still stand.
            try:
                deps.agent.after_decision(case_id, result.body["decision"], request_id)
            except Exception:
                if deps.settings.agent_raise_errors:
                    raise
                log.exception("could not resume the workflow for case %s", case_id)
        return {"decision": result.body, "case": render_case(deps, principal, case_id)}

    @app.post("/cases/{case_id}/review-request")
    def request_review(
        deps: Deps,
        principal: Caller,
        case_id: UUID,
        body: reviews.ReviewRequestInput,
        request: Request,
    ) -> dict[str, Any]:
        """A staff member sends a case to a supervisor, with a note. Safe to repeat."""
        marked = reviews.request_review(
            deps.db, deps.clock, principal, case_id, body, request.state.request_id
        )
        return {"case": render_case(deps, principal, case_id), "already_requested": not marked}

    # --- policies ---------------------------------------------------------------------

    @app.get("/policies")
    def list_policies(deps: Deps, principal: Caller) -> dict[str, Any]:
        with deps.db.tenant_tx(principal) as conn:
            return {"policies": policies.list_policies(conn)}

    @app.post("/policies/{policy_id}/versions", status_code=201)
    def publish_policy_version(
        deps: Deps, principal: Caller, policy_id: UUID, body: PublishVersionInput
    ) -> dict[str, Any]:
        if principal.role != "supervisor":
            raise Forbidden("Only a supervisor can publish a policy version")
        with deps.db.tenant_tx(principal) as conn:
            version = policies.publish_version(
                conn,
                principal,
                policy_id=policy_id,
                version_label=body.version_label,
                effective_from=body.effective_from,
                rules=body.rules,
                sections=[s.model_dump() for s in body.sections],
                now=deps.clock.now(),
            )
        # Make the new text searchable. An admin job: it writes rows for every audience.
        with deps.db.service_tx() as conn:
            index_policy_versions(conn, deps.embedder, [version["id"]])
        return {"version": version}

    # --- operations -------------------------------------------------------------------

    @app.get("/ops/summary")
    def ops_summary(deps: Deps, principal: Caller, window_days: int = 30) -> dict[str, Any]:
        """How this merchant's desk is doing: finished cases, review backlog, uncertain
        payments, latency and model cost, plus the latest eval run. Staff only."""
        window_days = max(1, min(window_days, 365))
        with deps.db.tenant_tx(principal) as conn:
            return {
                "summary": ops.summary(
                    conn, principal, now=deps.clock.now(), window_days=window_days
                )
            }

    # --- internal ---------------------------------------------------------------------

    @app.post("/internal/reconcile")
    def reconcile(
        deps: Deps,
        background: BackgroundTasks,
        authorization: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        """Run due execution and reconciliation work. Called every minute by pg_cron in
        production. Authenticated with a shared secret, never with a user token."""
        expected = f"Bearer {deps.settings.internal_token}"
        if not authorization or not hmac.compare_digest(authorization, expected):
            raise NotAuthenticated("Internal credentials required")
        # Runs every minute, so it also sweeps up any spans an earlier export missed.
        after_response(deps, background)
        return {"result": deps.execution.run_due()}

    # --- local development only -------------------------------------------------------

    if settings.environment == "local":

        @app.get("/dev/users")
        def dev_users(deps: Deps) -> dict[str, Any]:
            """Seeded people to sign in as. Exists only when RD_ENVIRONMENT is local."""
            with deps.db.service_tx() as conn:
                rows = conn.execute(
                    """
                    select u.email, u.display_name, m.slug as merchant, m.name as merchant_name,
                           ms.role::text as role
                    from memberships ms
                    join app_users u on u.id = ms.user_id
                    join merchants m on m.id = ms.merchant_id
                    union all
                    select u.email, u.display_name, m.slug, m.name, 'shopper'
                    from shoppers s
                    join app_users u on u.id = s.user_id
                    join merchants m on m.id = s.merchant_id
                    order by merchant, role, email
                    """
                ).fetchall()
            return {"users": rows}

        @app.post("/dev/session")
        def dev_session(deps: Deps, body: DevSessionInput) -> dict[str, Any]:
            """A signed token for a seeded user. Stands in for Supabase Auth until the
            deploy milestone. Exists only when RD_ENVIRONMENT is local."""
            with deps.db.service_tx() as conn:
                row = conn.execute(
                    """
                    select u.id as user_id, m.id as merchant_id,
                           exists (select 1 from memberships ms
                                   where ms.user_id = u.id and ms.merchant_id = m.id) as staff
                    from app_users u, merchants m
                    where u.email = %s and m.slug = %s
                    """,
                    (body.email, body.merchant),
                ).fetchone()
            if row is None:
                raise NotFound()
            token = mint_token(
                deps.settings,
                user_id=row["user_id"],
                merchant_id=row["merchant_id"],
                surface="staff" if row["staff"] else "shopper",
                now=datetime.now(UTC),
            )
            principal = authenticate(deps.settings, deps.db, f"Bearer {token}")
            return {"token": token, "role": principal.role, "merchant": principal.merchant_slug}

    return app
