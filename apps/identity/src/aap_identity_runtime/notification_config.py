"""Configuration isolated to the Notifications Redis publisher process."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret_file(path: str) -> SecretStr:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError()
        value = raw.decode("utf-8").strip()
        if not value:
            raise ValueError()
        return SecretStr(value)
    except (OSError, ValueError, UnicodeError):
        raise ValueError("Invalid notification publisher secret file") from None


class NotificationPublisherSettings(BaseSettings):
    """Only the Notifications owner DSN and Redis URL are required."""

    model_config = SettingsConfigDict(
        env_prefix="NOTIFICATION_PUBLISH_",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: SecretStr | None = None
    database_url_file: str | None = None
    redis_url: SecretStr | None = None
    redis_url_file: str | None = None

    claim_limit: int = Field(default=16, ge=1, le=100)
    lease_seconds: int = Field(default=60, ge=1, le=300)
    concurrency: int = Field(default=4, ge=1, le=4)
    redis_timeout_seconds: float = Field(default=5, gt=0, le=5)
    poll_interval_seconds: float = Field(default=1, gt=0, le=60)
    connect_timeout_seconds: float = Field(default=5, gt=0, le=30)
    command_timeout_seconds: float = Field(default=10, gt=0, le=60)

    @model_validator(mode="after")
    def resolve_sources(self) -> NotificationPublisherSettings:
        for name in ("database_url", "redis_url"):
            value = getattr(self, name)
            path = getattr(self, name + "_file")
            if (value is None) == (path is None):
                raise ValueError(
                    f"Set exactly one notification publisher {name} source"
                )
            if path is not None:
                value = _read_secret_file(path)
                setattr(self, name, value)
            if value is None or not value.get_secret_value().strip():
                raise ValueError(f"Empty notification publisher {name}")
        return self
