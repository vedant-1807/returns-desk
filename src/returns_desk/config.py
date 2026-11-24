from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings. Every value can be overridden with an RD_ environment variable."""

    model_config = SettingsConfigDict(env_prefix="RD_", env_file=".env", extra="ignore")

    # Four connections, four jobs. See db/migrations/0001_tenancy.sql for the roles.
    admin_dsn: str = "postgresql://postgres:postgres@localhost:54329/returns_desk"
    app_dsn: str = "postgresql://rd_app:rd_app_local@localhost:54329/returns_desk"
    service_dsn: str = "postgresql://rd_service:rd_service_local@localhost:54329/returns_desk"
    processor_dsn: str = "postgresql://rd_processor:rd_processor_local@localhost:54329/returns_desk"
    # Workflow checkpoints only. This role can reach nothing else.
    workflow_dsn: str = "postgresql://rd_workflow:rd_workflow_local@localhost:54329/returns_desk"

    # "local" allows the built-in development secrets below. Anything else refuses to start
    # until RD_JWT_SECRET and RD_INTERNAL_TOKEN are set to real values.
    environment: str = "local"

    jwt_secret: str = "local-dev-secret-change-me-0123456789abcdef"
    jwt_audience: str = "authenticated"
    internal_token: str = "local-internal-token-change-me"

    # Execution worker: how long one worker owns an action, and how retries back off.
    lease_seconds: int = 60
    retry_base_seconds: int = 30
    retry_max_seconds: int = 900

    pool_max_size: int = 5

    # Browser origins allowed to call the API (the two screens in development).
    cors_origins: str = "http://localhost:3000"
    # Host names POST /mcp answers to. Empty turns the host check off; the endpoint still
    # requires a bearer token on every call.
    mcp_allowed_hosts: str = ""
    # Tests set this so a bug in the agent fails loudly instead of becoming a hand-off.
    agent_raise_errors: bool = False

    # --- models ---------------------------------------------------------------------------
    # Routes are tried in this order; a 429 or timeout on one fails over to the next.
    # Every route speaks the OpenAI-compatible chat API, so one client serves them all.
    #   ollama      local models, free, no key (development and local eval runs)
    #   groq        free tier, needs RD_GROQ_API_KEY
    #   openrouter  cheap paid or free models, needs RD_OPENROUTER_API_KEY
    #   keyword     no model at all: a keyword stand-in, so a fresh checkout runs with zero
    #               setup. Fine for tests and a first look, not for judging quality.
    model_routes: str = "keyword"

    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model_low: str = "llama3.1:8b"
    ollama_model_strong: str = "llama3.1:8b"

    groq_api_key: str = ""
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model_low: str = "openai/gpt-oss-20b"
    groq_model_strong: str = "openai/gpt-oss-120b"

    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model_low: str = "openai/gpt-oss-20b"
    openrouter_model_strong: str = "openai/gpt-oss-120b"

    # Per shopper turn: at most this many model attempts, inside this combined deadline.
    # Escalation and failover share the budget. Local models need a longer deadline than the
    # 10 seconds the spec sets for hosted ones.
    model_attempts_per_turn: int = 2
    model_deadline_seconds: float = 10.0
    # When a route answers "rate limited" and says how long to wait, the gateway may wait
    # up to this many seconds and ask the same route again. Zero means never wait and fail
    # over at once, which is right for a live chat. Evals turn it on, because a free plan
    # allows only a few thousand tokens a minute.
    model_rate_limit_wait_seconds: float = 0.0

    # --- embeddings -----------------------------------------------------------------------
    #   hash        deterministic and offline; tests and CI
    #   ollama      nomic-embed-text on the local server
    #   openrouter  baai/bge-base-en-v1.5
    # All of them produce 768 numbers, which is what the policy_chunks table stores.
    embedder: str = "hash"
    ollama_embedding_model: str = "nomic-embed-text"
    openrouter_embedding_model: str = "baai/bge-base-en-v1.5"


def require_real_secrets(settings: Settings) -> None:
    """Stop a deployed instance from running on the development secrets in this file."""
    if settings.environment == "local":
        return
    for name in ("jwt_secret", "internal_token"):
        if getattr(settings, name) == Settings.model_fields[name].default:
            raise RuntimeError(
                f"RD_{name.upper()} must be set when RD_ENVIRONMENT is {settings.environment!r}"
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
