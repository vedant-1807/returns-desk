from __future__ import annotations

import logging
import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any
from uuid import UUID

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from returns_desk.agent.graph import TurnContext, build_graph
from returns_desk.db import Principal
from returns_desk.tools.client import ToolRuntime
from returns_desk.tools.server import build_tool_server

if TYPE_CHECKING:
    from returns_desk.container import Container

log = logging.getLogger(__name__)


class AgentRuntime:
    """Runs the workflow for one case at a time and keeps its checkpoints in Postgres.

    Checkpoints live in the `workflow` schema under a role that can reach nothing else. A
    thread is only touched after the caller's access to the case has been checked under
    row-level security by the API, and its ID carries the merchant and the case.
    """

    def __init__(self, container: Container) -> None:
        self._container = container
        self._pool: ConnectionPool[Any] = ConnectionPool(
            container.settings.workflow_dsn,
            min_size=1,
            max_size=container.settings.pool_max_size,
            name="rd-workflow",
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
                "options": "-c search_path=workflow",
            },
            open=True,
        )
        self._checkpointer = PostgresSaver(self._pool)  # type: ignore[arg-type]
        self._graph = build_graph().compile(checkpointer=self._checkpointer)
        self._tools = ToolRuntime()
        self._locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)
        self._locks_guard = threading.Lock()

    def close(self) -> None:
        self._tools.close()
        self._pool.close()

    # --- public -----------------------------------------------------------------------

    def shopper_turn(
        self, principal: Principal, case_id: UUID, message: str, request_id: str | None
    ) -> None:
        """Run the workflow for a new shopper message: start it, or resume it if it was
        waiting for this shopper."""
        thread = self._thread(principal.merchant_id, case_id)
        with self._serialized(thread), self._context(principal, request_id) as ctx:
            config = {"configurable": {"thread_id": thread}}
            waiting, turn = self._waiting(config)
            try:
                if waiting == "customer":
                    self._graph.invoke(Command(resume={"message": message}), config, context=ctx)
                elif waiting == "review":
                    # Not ours to resume. The API answers with the current status instead.
                    return
                else:
                    self._graph.invoke(
                        {
                            "case_id": str(case_id),
                            "message": message,
                            "turn": turn + 1,
                            "step": "",
                            "pending": None,
                            "quote": None,
                        },
                        config,
                        context=ctx,
                    )
            except Exception:
                # A broken run must not leave a half-finished thread that the next message
                # would resume from the middle. Start clean next time.
                self._checkpointer.delete_thread(thread)
                raise

    def after_decision(self, case_id: UUID, decision: str, request_id: str | None) -> bool:
        """Resume a thread that was waiting for a supervisor. Runs as the case's own
        shopper, so only customer-visible data can reach the reply. Returns False when the
        thread was not waiting for a review."""
        owner = self._owner_principal(case_id)
        if owner is None:
            return False
        thread = self._thread(owner.merchant_id, case_id)
        with self._serialized(thread), self._context(owner, request_id) as ctx:
            config = {"configurable": {"thread_id": thread}}
            waiting, _ = self._waiting(config)
            if waiting != "review":
                return False
            self._graph.invoke(Command(resume={"decision": decision}), config, context=ctx)
            return True

    def waiting_for(self, merchant_id: UUID, case_id: UUID) -> str | None:
        """'customer', 'review' or None: what this case's thread is paused on."""
        config = {"configurable": {"thread_id": self._thread(merchant_id, case_id)}}
        return self._waiting(config)[0]

    def state(self, merchant_id: UUID, case_id: UUID) -> dict[str, Any]:
        config = {"configurable": {"thread_id": self._thread(merchant_id, case_id)}}
        return dict(self._graph.get_state(config).values)

    # --- internals --------------------------------------------------------------------

    @staticmethod
    def _thread(merchant_id: UUID, case_id: UUID) -> str:
        return f"{merchant_id}:{case_id}"

    def _waiting(self, config: dict[str, Any]) -> tuple[str | None, int]:
        snapshot = self._graph.get_state(config)
        turn = int(snapshot.values.get("turn") or 0)
        for task in snapshot.tasks:
            for pending in task.interrupts:
                value = pending.value if isinstance(pending.value, dict) else {}
                return value.get("waiting_for"), turn
        return None, turn

    @contextmanager
    def _serialized(self, thread: str) -> Iterator[None]:
        """One run per case at a time in this process."""
        with self._locks_guard:
            lock = self._locks[thread]
        with lock:
            yield

    @contextmanager
    def _context(self, principal: Principal, request_id: str | None) -> Iterator[TurnContext]:
        container = self._container
        server = build_tool_server(container, principal, request_id=request_id)
        with self._tools.session(server) as tools:
            yield TurnContext(
                container=container,
                principal=principal,
                tools=tools,
                budget=container.models.new_budget(),
                request_id=request_id,
            )

    def _owner_principal(self, case_id: UUID) -> Principal | None:
        with self._container.db.service_tx() as conn:
            row = conn.execute(
                """
                select c.merchant_id, c.shopper_id, s.user_id, m.timezone, m.slug,
                       m.permission_version
                from cases c
                join shoppers s on s.id = c.shopper_id
                join merchants m on m.id = c.merchant_id
                where c.id = %s
                """,
                (case_id,),
            ).fetchone()
        if row is None:
            return None
        return Principal(
            user_id=row["user_id"],
            merchant_id=row["merchant_id"],
            role="shopper",
            shopper_id=row["shopper_id"],
            merchant_timezone=row["timezone"],
            merchant_slug=row["slug"],
            permission_version=row["permission_version"],
        )
