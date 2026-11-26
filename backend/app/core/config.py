import secrets
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    app_log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://taskflow:taskflow@localhost:5432/taskflow"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret_key: SecretStr = Field(
        default_factory=lambda: SecretStr(secrets.token_urlsafe(48)),
        repr=False,
    )
    jwt_issuer: str = "taskflow-ai"
    access_token_expire_minutes: int = Field(default=15, gt=0, le=60)
    refresh_token_expire_days: int = Field(default=30, gt=0, le=90)
    auth_rate_limit_per_minute: int = Field(default=10, gt=0, le=100)
    auth_cookie_secure: bool = False
    planning_provider: Literal["openai", "anthropic"] = "openai"
    planning_model: str | None = Field(default=None, min_length=1, max_length=120)
    openai_api_key: SecretStr | None = Field(default=None, repr=False)
    anthropic_api_key: SecretStr | None = Field(default=None, repr=False)

    model_config = SettingsConfigDict(
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
    )

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.app_env.casefold() == "production":
            secret = self.jwt_secret_key.get_secret_value()
            if "jwt_secret_key" not in self.model_fields_set:
                raise ValueError("JWT_SECRET_KEY must be configured explicitly in production.")
            if len(secret.encode("utf-8")) < 32:
                raise ValueError("JWT_SECRET_KEY must contain at least 32 bytes in production.")
            if not self.auth_cookie_secure:
                raise ValueError("AUTH_COOKIE_SECURE must be enabled in production.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
