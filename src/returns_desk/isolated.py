"""Running the service in a database of its own.

Evals, replays and the recorded walkthrough each reset and reseed their world over and over.
They do that in a separate database on the same server, created and migrated on demand, so
none of them can touch the development data.
"""

from __future__ import annotations

from typing import Any

import psycopg
from psycopg.conninfo import make_conninfo

from returns_desk.config import Settings
from returns_desk.migrate import migrate

_DSNS = ("admin_dsn", "app_dsn", "service_dsn", "processor_dsn", "workflow_dsn")


def isolated_settings(base: Settings, database: str, **overrides: Any) -> Settings:
    """Settings pointed at `database`, which is created and migrated if needed."""
    with psycopg.connect(base.admin_dsn, autocommit=True) as conn:
        exists = conn.execute(
            "select 1 from pg_database where datname = %s", (database,)
        ).fetchone()
        if not exists:
            conn.execute(f'create database "{database}"')
    settings = base.model_copy(
        update={name: make_conninfo(getattr(base, name), dbname=database) for name in _DSNS}
        | {"environment": "local", "agent_raise_errors": False}
        | overrides
    )
    migrate(settings)
    return settings
