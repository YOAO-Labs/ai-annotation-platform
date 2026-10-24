"""Environment settings for the independent Notifications WebSocket owner."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret_file(path: str) -> SecretStr:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError
        value = raw.decode("utf-8").strip()
        if not value:
            raise ValueError
        return SecretStr(value)
    except (OSError, ValueError, UnicodeError):
        raise ValueError("Invalid notification socket secret file") from None


def _resolve_secret(
    name: str,
    value: SecretStr | None,
    path: str | None,
) -> SecretStr:
    if (value is None) == (path is None):
        raise ValueError(f"Set exactly one notification socket {name} source")
    if path is not None:
        value = _read_secret_file(path)
    if value is None or not value.get_secret_value().strip():
        raise ValueError(f"Empty notification socket {name}")
    return value


class NotificationSocketSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NOTIFICATION_SOCKET_",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: SecretStr | None = None
    database_url_file: str | None = None
    redis_url: SecretStr | None = None
    redis_url_file: str | None = None
    identity_url: str
    identity_token: SecretStr | None = None
    identity_token_file: str | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=8180, ge=1, le=65535)

    @model_validator(mode="after")
    def resolve_sources(self) -> NotificationSocketSettings:
        self.database_url = _resolve_secret(
            "database URL", self.database_url, self.database_url_file
        )
        self.redis_url = _resolve_secret(
            "Redis URL", self.redis_url, self.redis_url_file
        )
        self.identity_token = _resolve_secret(
            "Identity token", self.identity_token, self.identity_token_file
        )

        try:
            parsed = urlsplit(self.identity_url)
            _ = parsed.port
            valid_identity_url = (
                self.identity_url == self.identity_url.strip()
                and parsed.scheme in {"http", "https"}
                and parsed.netloc
                and parsed.hostname is not None
                and parsed.username is None
                and parsed.password is None
            )
        except ValueError:
            valid_identity_url = False
        if not valid_identity_url:
            raise ValueError("Invalid notification socket Identity URL")
        if not self.host.strip():
            raise ValueError("Invalid notification socket host")
        return self
