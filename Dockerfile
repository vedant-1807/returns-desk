# The API, the agent workflow, the MCP tool server and the model gateway, in one image.
# Built for Render's free web service; it runs anywhere a container does.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    RD_MIGRATIONS_DIR=/app/db/migrations

# Dependencies first, so a code change does not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY db ./db
RUN uv sync --frozen --no-dev

# Run as an ordinary user.
RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8000
# Apply pending migrations, then serve. Render sets PORT.
CMD ["sh", "-c", ".venv/bin/returns-desk migrate && exec .venv/bin/uvicorn --factory returns_desk.api:create_app --host 0.0.0.0 --port ${PORT:-8000}"]
