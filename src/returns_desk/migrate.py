from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from returns_desk.config import Settings

KNOWN_ROLES = ("rd_app", "rd_service", "rd_processor", "rd_workflow")


def migrations_dir() -> Path:
    override = os.environ.get("RD_MIGRATIONS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[2] / "db" / "migrations"


def migrate(settings: Settings) -> list[str]:
    """Apply pending SQL migrations in order, each in its own transaction. Returns the
    versions applied by this call."""
    applied_now: list[str] = []
    with psycopg.connect(settings.admin_dsn, autocommit=True) as conn:
        conn.execute(
            """
            create table if not exists schema_migrations (
              version text primary key,
              applied_at timestamptz not null default now()
            )
            """
        )
        applied = {row[0] for row in conn.execute("select version from schema_migrations")}
        for path in sorted(migrations_dir().glob("*.sql")):
            if path.stem in applied:
                continue
            with conn.transaction():
                conn.execute(path.read_text(encoding="utf-8"))  # type: ignore[arg-type]
                conn.execute("insert into schema_migrations (version) values (%s)", (path.stem,))
            applied_now.append(path.stem)
        conn.execute("revoke all on table schema_migrations from rd_app, rd_service, rd_processor")
        _set_role_passwords(conn, settings)
    _setup_workflow_checkpoints(settings)
    return applied_now


def _setup_workflow_checkpoints(settings: Settings) -> None:
    """Create or upgrade the LangGraph checkpoint tables inside the workflow schema, then
    let the workflow role use them. The library owns the table definitions; this only
    decides where they live and who may touch them."""
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row

    with psycopg.connect(
        settings.admin_dsn,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,
        options="-c search_path=workflow",
    ) as conn:
        PostgresSaver(conn).setup()  # type: ignore[arg-type]
        conn.execute(
            "grant select, insert, update, delete on all tables in schema workflow to rd_workflow"
        )


def _set_role_passwords(conn: psycopg.Connection, settings: Settings) -> None:
    """Give the three application roles the passwords from the environment. Migrations
    create the roles without a login so that no secret ever sits in a SQL file."""
    for dsn in (
        settings.app_dsn,
        settings.service_dsn,
        settings.processor_dsn,
        settings.workflow_dsn,
    ):
        info = conninfo_to_dict(dsn)
        # Supabase pooler usernames look like "rd_app.<project-ref>".
        role = str(info.get("user", "")).split(".")[0]
        password = info.get("password")
        if role not in KNOWN_ROLES or not password:
            continue
        conn.execute(
            sql.SQL("alter role {} with login password {}").format(
                sql.Identifier(role), sql.Literal(str(password))
            )
        )
