from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Infrastructure
    database_url: str = "sqlite:///./taskflow.db"
    redis_url: str = "redis://localhost:6379/0"

    # Authentication. `algorithm` is accepted for compatibility with older .env files.
    jwt_secret_key: str
    jwt_algorithm: str = Field(
        default="HS256",
        validation_alias=AliasChoices("JWT_ALGORITHM", "algorithm"),
    )
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7

    # Bearer token required by /api/v1/metrics. When unset the endpoint is disabled.
    metrics_token: str | None = None

    # Request limits
    max_request_bytes: int = 1_048_576
    max_payload_bytes: int = 65_536
    rate_limit_submit_per_minute: int = 60
    rate_limit_retry_per_minute: int = 20

    # Retry policy: delay = min(max_delay, base * 2^(attempt - 1)) + jitter
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 300.0
    retry_jitter_seconds: float = 1.0

    # Workers
    worker_concurrency: int = 4
    worker_poll_interval_seconds: float = 0.5
    worker_lease_seconds: int = 30
    worker_heartbeat_interval_seconds: float = 5.0
    worker_shutdown_grace_seconds: float = 30.0
    # Prometheus endpoint for worker, dispatcher and scheduler processes (0 disables).
    metrics_port: int = 9101
    # Relative weights for draining the high / normal / low priority queues.
    queue_weights: tuple[int, int, int] = (6, 3, 1)

    # Outbox dispatcher
    dispatcher_batch_size: int = 100
    dispatcher_interval_seconds: float = 0.5

    # Scheduler / recovery
    scheduler_interval_seconds: float = 5.0
    queued_redispatch_after_seconds: int = 300
    outbox_retention_days: int = 7

    log_level: str = "INFO"
    log_json: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
