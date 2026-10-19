from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IDENTITY_", extra="ignore")

    database_url: SecretStr | None = None
    database_url_file: str | None = None
    token: SecretStr | None = None
    token_file: str | None = None
    signing_secret: SecretStr | None = None
    signing_secret_file: str | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=8170, ge=1, le=65535)
    max_concurrency: int = Field(default=8, ge=1, le=64)
    request_timeout_seconds: float = Field(default=15, gt=0, le=120)
    connect_timeout_seconds: float = Field(default=5, gt=0, le=30)
    command_timeout_seconds: float = Field(default=10, gt=0, le=60)

    @model_validator(mode="after")
    def secrets(self) -> Settings:
        for field in ("database_url", "token", "signing_secret"):
            value = getattr(self, field)
            path = getattr(self, field + "_file")
            if (value is None) == (path is None):
                raise ValueError(f"Set exactly one {field} source")
            if path is not None:
                try:
                    with Path(path).open("rb") as stream:
                        raw = stream.read(4097)
                    if len(raw) > 4096:
                        raise ValueError()
                    value = SecretStr(raw.decode().strip())
                except (OSError, ValueError, UnicodeError):
                    raise ValueError(f"Invalid {field} file") from None
                setattr(self, field, value)
            if value is None or not value.get_secret_value().strip():
                raise ValueError(f"Empty {field}")
        return self
