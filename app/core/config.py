"""Application configuration.

Every value is environment driven. Secrets are never hardcoded and never
carry a usable production default.
"""

from __future__ import annotations

import decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["local", "test", "staging", "production"]

_ROUNDING_MODES: dict[str, str] = {
    "ROUND_HALF_UP": decimal.ROUND_HALF_UP,
    "ROUND_HALF_EVEN": decimal.ROUND_HALF_EVEN,
    "ROUND_DOWN": decimal.ROUND_DOWN,
    "ROUND_UP": decimal.ROUND_UP,
    "ROUND_CEILING": decimal.ROUND_CEILING,
    "ROUND_FLOOR": decimal.ROUND_FLOOR,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application --------------------------------------------------------
    app_env: AppEnv = "local"
    app_name: str = "ERP Financial Core"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"

    secret_key: str = Field(min_length=32)

    # --- Database -----------------------------------------------------------
    database_url: PostgresDsn
    database_pool_size: int = 10
    database_max_overflow: int = 10
    database_echo: bool = False
    database_statement_timeout_ms: int = 30_000

    # --- Redis --------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # --- Auth ---------------------------------------------------------------
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 14
    jwt_algorithm: str = "HS256"

    # --- HTTP ---------------------------------------------------------------
    cors_origins: list[str] = Field(default_factory=list)
    trusted_hosts: list[str] = Field(default_factory=lambda: ["*"])
    openapi_enabled: bool = True
    max_request_body_bytes: int = 2 * 1024 * 1024
    slow_request_ms: int = 1000

    rate_limit_enabled: bool = True
    rate_limit_requests: int = 300
    rate_limit_window_seconds: int = 60

    # --- Accounting ---------------------------------------------------------
    money_rounding: str = "ROUND_HALF_UP"
    journal_number_prefix: str = "JE"

    # --- Observability ------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    metrics_enabled: bool = True
    otel_enabled: bool = False
    otel_service_name: str = "erp-financial-core"
    otel_exporter_otlp_endpoint: str | None = None
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.0

    # --- Seed ---------------------------------------------------------------
    seed_admin_email: str = "admin@example.com"
    seed_admin_password: str | None = None
    seed_accountant_password: str | None = None
    seed_auditor_password: str | None = None
    seed_viewer_password: str | None = None

    @field_validator("money_rounding")
    @classmethod
    def _validate_rounding(cls, value: str) -> str:
        if value not in _ROUNDING_MODES:
            raise ValueError(
                f"MONEY_ROUNDING must be one of {sorted(_ROUNDING_MODES)}, got {value!r}"
            )
        return value

    @property
    def rounding_mode(self) -> str:
        """The `decimal` rounding constant matching `money_rounding`."""
        return _ROUNDING_MODES[self.money_rounding]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def sync_database_url(self) -> str:
        """Sync (psycopg) DSN — used by Alembic, which runs migrations sync."""
        return (
            str(self.database_url)
            .replace("+asyncpg", "")
            .replace("postgresql://", "postgresql+psycopg://", 1)
        )

    @property
    def async_database_url(self) -> str:
        url = str(self.database_url)
        if "+" not in url.split("://", 1)[0]:
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


settings = get_settings()
