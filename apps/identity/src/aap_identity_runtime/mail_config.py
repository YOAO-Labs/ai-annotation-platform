"""Environment and mounted-file settings for the isolated mail process."""

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
        raise ValueError("Invalid mail worker secret file") from None


class MailWorkerSettings(BaseSettings):
    """Configuration independent of the Identity API and its AUTH secrets."""

    model_config = SettingsConfigDict(
        env_prefix="IDENTITY_MAIL_",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: SecretStr | None = None
    database_url_file: str | None = None
    settings_database_url: SecretStr | None = None
    settings_database_url_file: str | None = None
    encryption_key: SecretStr | None = None
    encryption_key_file: str | None = None

    smtp_host: str = Field(default="", max_length=255)
    smtp_port: int = Field(default=0, ge=0, le=65535)
    smtp_user: str = Field(default="", max_length=255, repr=False)
    smtp_password: SecretStr | None = None
    smtp_password_file: str | None = None
    smtp_from: str = Field(default="", max_length=320)

    host: str = "127.0.0.1"
    port: int = Field(default=8171, ge=1, le=65535)
    poll_interval_seconds: float = Field(default=5, gt=0, le=300)
    lease_seconds: int = Field(default=60, ge=1, le=300)
    retry_delay_seconds: int = Field(default=30, ge=1, le=300)
    connect_timeout_seconds: float = Field(default=5, gt=0, le=30)
    command_timeout_seconds: float = Field(default=10, gt=0, le=60)

    @model_validator(mode="after")
    def resolve_sources(self) -> MailWorkerSettings:
        for name in ("database_url", "settings_database_url", "encryption_key"):
            value = getattr(self, name)
            path = getattr(self, name + "_file")
            if (value is None) == (path is None):
                raise ValueError(f"Set exactly one mail {name} source")
            if path is not None:
                value = _read_secret_file(path)
                setattr(self, name, value)
            if value is None or not value.get_secret_value().strip():
                raise ValueError(f"Empty mail {name}")

        password_path = self.smtp_password_file
        if password_path is not None:
            if self.smtp_password is not None:
                raise ValueError("Set at most one mail smtp_password source")
            self.smtp_password = _read_secret_file(password_path)
        if self.smtp_password is None:
            self.smtp_password = SecretStr("")

        for name in ("smtp_host", "smtp_user", "smtp_from", "host"):
            value = getattr(self, name)
            if "\r" in value or "\n" in value or "\x00" in value:
                raise ValueError(f"Invalid mail {name}")
        if not self.host.strip():
            raise ValueError("Empty mail worker host")
        return self
