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
    #   groq        free plan, needs RD_GROQ_API_KEY
    #   groq_alt    a second model family on the same free Groq account. Groq's limits are
    #               per model, so this is a real second route with its own allowance.
    #   openrouter  paid, needs RD_OPENROUTER_API_KEY and credits
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
    groq_alt_model_low: str = "qwen/qwen3.8-27b"
    groq_alt_model_strong: str = "qwen/qwen3.8-27b"

    # How a model call picks its tier. The eval suite compares these on identical cases.
    #   routed         the low-cost model first; the stronger one only for an answer that
    #                  settled nothing or that conflicts with the shopper's own words
    #   always_strong  the stronger model for every call
    #   always_low     the low-cost model for every call, no escalation
    model_router: str = "routed"

    # Daily model budget per merchant in tokens (input plus output), counted on the
    # merchant's local calendar day. 0 means no cap. A merchant row can set its own cap.
    # At the cap no model is called: the agent reads short replies by rule, quotes policy
    # text as it stands, or hands the case to staff.
    model_daily_token_cap: int = 0

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

    # --- tracing --------------------------------------------------------------------------
    # Every case has a trace at GET /cases/{id}/trace, built from the audit trail. With
    # both keys set, the same spans are also sent to Langfuse (free Hobby plan works) and
    # staff see a link on each case. Nothing is sent when the keys are empty.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"
    # Only needed for trace links; looked up from the keys when left empty.
    langfuse_project_id: str = ""

    # --- embeddings -----------------------------------------------------------------------
    #   hash        deterministic and offline; tests and CI
    #   ollama      nomic-embed-text on the local server
    #   openrouter  baai/bge-base-en-v1.5
    # All of them produce 768 numbers, which is what the policy_chunks table stores.
    #   openai      any hosted API in the OpenAI embeddings format (set the four values
    #               below); for a deployment that cannot run a local model
    embedder: str = "hash"
    ollama_embedding_model: str = "nomic-embed-text"
    openrouter_embedding_model: str = "baai/bge-base-en-v1.5"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = ""
    # Sent as `dimensions` to models that can shorten their vectors. 0 sends nothing.
    embedding_dimensions: int = 0


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
