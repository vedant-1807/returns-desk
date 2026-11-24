"""Serve the API for the browser tests.

It runs against its own database (returns_desk_e2e), freshly migrated and seeded, with the
keyword stand-in as the model and hashed embeddings, so the browser tests need no model,
no network and never touch the development data.

    uv run python scripts/e2e_api.py            # serves on http://localhost:8010
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import uvicorn

from returns_desk.api.app import create_app
from returns_desk.config import Settings
from returns_desk.container import build_container
from returns_desk.isolated import isolated_settings
from returns_desk.seeds import seed

PORT = int(os.environ.get("RD_E2E_API_PORT", "8010"))
WEB_ORIGIN = os.environ.get("RD_E2E_WEB_ORIGIN", "http://localhost:3010")


def main() -> None:
    settings = isolated_settings(
        Settings(),
        "returns_desk_e2e",
        model_routes="keyword",
        embedder="hash",
        cors_origins=WEB_ORIGIN,
        payment_provider="sandbox",
        langfuse_public_key="",
        langfuse_secret_key="",
    )
    container = build_container(settings)
    seed(
        settings.admin_dsn,
        container.processor,  # type: ignore[arg-type]
        fixture=os.environ.get("RD_E2E_FIXTURE", "two_jackets"),
        now=datetime.now(UTC),
        embedder=container.embedder,
    )
    uvicorn.run(create_app(container), host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
