from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from returns_desk.clock import Clock, SystemClock
from returns_desk.config import Settings, get_settings, require_real_secrets
from returns_desk.db import Database
from returns_desk.processor.base import PaymentProcessor
from returns_desk.processor.sandbox import SandboxProcessor
from returns_desk.retrieval.embedders import Embedder, build_embedder
from returns_desk.retrieval.search import PolicySearch
from returns_desk.services.execution import ExecutionService

if TYPE_CHECKING:
    from returns_desk.agent.runtime import AgentRuntime
    from returns_desk.models.gateway import ModelGateway
    from returns_desk.tracing.langfuse import LangfuseExporter


@dataclass
class Container:
    """Everything a request handler needs. Built once per process."""

    settings: Settings
    db: Database
    clock: Clock
    processor: PaymentProcessor
    execution: ExecutionService
    embedder: Embedder
    policy_search: PolicySearch
    models: ModelGateway
    tracer: LangfuseExporter
    agent: AgentRuntime = field(init=False)
    _closers: list[Any] = field(default_factory=list, repr=False)

    def close(self) -> None:
        for closer in reversed(self._closers):
            # Closing must not mask the error that got us here.
            with contextlib.suppress(Exception):
                closer()
        self.db.close()
        for resource in (self.processor, self.embedder, self.models, self.tracer):
            close = getattr(resource, "close", None)
            if callable(close):
                close()


def build_container(
    settings: Settings | None = None,
    *,
    clock: Clock | None = None,
    processor: PaymentProcessor | None = None,
    embedder: Embedder | None = None,
    models: ModelGateway | None = None,
    tracer: LangfuseExporter | None = None,
) -> Container:
    from returns_desk.agent.runtime import AgentRuntime
    from returns_desk.models.gateway import build_gateway
    from returns_desk.tracing.langfuse import LangfuseExporter

    settings = settings or get_settings()
    require_real_secrets(settings)
    clock = clock or SystemClock()
    db = Database(settings)
    processor = processor or SandboxProcessor(settings.processor_dsn, clock)
    embedder = embedder or build_embedder(settings)
    container = Container(
        settings=settings,
        db=db,
        clock=clock,
        processor=processor,
        execution=ExecutionService(db, processor, clock, settings),
        embedder=embedder,
        policy_search=PolicySearch(embedder, clock),
        models=models or build_gateway(settings, clock),
        tracer=tracer or LangfuseExporter(db, settings),
    )
    container.agent = AgentRuntime(container)
    container._closers.append(container.agent.close)
    return container
