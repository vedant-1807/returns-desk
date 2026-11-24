from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from returns_desk.config import Settings

Role = Literal["shopper", "associate", "supervisor"]
Conn = psycopg.Connection[dict[str, Any]]


@dataclass(frozen=True)
class Principal:
    """Who is calling, as verified by the server. Nothing here is taken from the model or
    from unverified request fields: the role and shopper ID come from the database."""

    user_id: UUID
    merchant_id: UUID
    role: Role
    shopper_id: UUID | None
    merchant_timezone: str
    merchant_slug: str
    # Moves whenever a staff role at this merchant changes. Part of every cache key.
    permission_version: int = 0

    @property
    def is_staff(self) -> bool:
        return self.role in ("associate", "supervisor")


def _pool(dsn: str, max_size: int, name: str) -> ConnectionPool[Conn]:
    return ConnectionPool(
        dsn,
        min_size=1,
        max_size=max_size,
        name=name,
        kwargs={"row_factory": dict_row, "options": "-c timezone=UTC"},
        open=True,
    )


class Database:
    """Two pools with different powers.

    `tenant_tx` is what request handlers use: the rd_app role, bound by row-level security,
    with the caller's claims set for exactly one transaction.

    `service_tx` is for admin jobs (refund execution and reconciliation). It bypasses
    row-level security and must never be driven by request input alone.
    """

    def __init__(self, settings: Settings) -> None:
        self._app = _pool(settings.app_dsn, settings.pool_max_size, "rd-app")
        self._service = _pool(settings.service_dsn, settings.pool_max_size, "rd-service")

    def close(self) -> None:
        self._app.close()
        self._service.close()

    @contextmanager
    def tenant_tx(self, principal: Principal) -> Iterator[Conn]:
        with self._app.connection() as conn, conn.transaction():
            conn.execute(
                """
                select set_config('app.user_id', %s, true),
                       set_config('app.merchant_id', %s, true),
                       set_config('app.role', %s, true),
                       set_config('app.shopper_id', %s, true)
                """,
                (
                    str(principal.user_id),
                    str(principal.merchant_id),
                    principal.role,
                    str(principal.shopper_id) if principal.shopper_id else "",
                ),
            )
            yield conn

    @contextmanager
    def service_tx(self) -> Iterator[Conn]:
        with self._service.connection() as conn, conn.transaction():
            yield conn

    def auth_context(self, user_id: UUID, merchant_id: UUID) -> dict[str, Any] | None:
        """The caller's relationship to one merchant. Runs before any claims exist."""
        with self._app.connection() as conn, conn.transaction():
            return conn.execute(
                "select * from auth_context(%s, %s)", (user_id, merchant_id)
            ).fetchone()

    def merchant_id(self, slug: str) -> UUID | None:
        """A merchant's ID from its slug, or None. Runs before any claims exist."""
        with self._app.connection() as conn, conn.transaction():
            row = conn.execute("select auth_merchant_id(%s) as id", (slug,)).fetchone()
        return row["id"] if row else None

    def relationships(self, user_id: UUID) -> list[dict[str, Any]]:
        """Every merchant this user can sign in to, and as what."""
        with self._app.connection() as conn, conn.transaction():
            return conn.execute("select * from auth_relationships(%s)", (user_id,)).fetchall()
