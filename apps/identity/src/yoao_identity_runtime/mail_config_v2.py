"""Environment and mounted-file settings for the isolated Mail v2 process."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_secret_file(path: str, *, max_bytes: int | None = 4096) -> SecretStr:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(max_bytes + 1) if max_bytes is not None else stream.read()
        if max_bytes is not None and len(raw) > max_bytes:
            raise ValueError()
        value = raw.decode("utf-8").strip()
        if not value:
            raise ValueError()
        return SecretStr(value)
    except (OSError, ValueError, UnicodeError):
        raise ValueError("Invalid Mail v2 worker secret file") from None


def _postgres_binding(secret: SecretStr) -> tuple[str, tuple[str, int, str]]:
    value = secret.get_secret_value()
    try:
        url = urlsplit(value)
        if (
            url.scheme != "postgresql"
            or not (url.hostname and url.username and url.path.strip("/"))
            or url.fragment
            or any(ord(character) < 33 for character in value)
            or any(
                key in {"host", "port", "user", "password", "dbname", "database"}
                for key, _ in parse_qsl(url.query, keep_blank_values=True)
            )
        ):
            raise ValueError()
        return unquote(url.username), (
            url.hostname,
            url.port or 5432,
            unquote(url.path.removeprefix("/")),
        )
    except ValueError:
        raise ValueError("Invalid Mail v2 PostgreSQL LOGIN DSN") from None


class MailV2WorkerSettings(BaseSettings):
    """Mail v2 needs only its owner DSN, Settings DSN, Fernet key and SMTP."""

    model_config = SettingsConfigDict(
        env_prefix="IDENTITY_MAIL_V2_",
        extra="ignore",
        case_sensitive=False,
    )

    database_url: SecretStr | None = None
    database_url_file: str | None = None
    settings_database_url: SecretStr | None = None
    settings_database_url_file: str | None = None
    encryption_key: SecretStr | None = None
    encryption_key_file: str | None = None

    smtp_host: str = ""
    smtp_port: int = Field(default=0, ge=0, le=65535)
    smtp_user: str = Field(default="", repr=False)
    smtp_password: SecretStr | None = None
    smtp_password_file: str | None = None

    smtp_from: str = ""
    host: str = "127.0.0.1"
    port: int = Field(default=8171, ge=1, le=65535)
    poll_interval_seconds: float = Field(default=5, gt=0, le=300)
    lease_seconds: int = Field(default=60, ge=1, le=300)
    retry_delay_seconds: int = Field(default=30, ge=1, le=300)
    connect_timeout_seconds: float = Field(default=5, gt=0, le=30)
    command_timeout_seconds: float = Field(default=10, gt=0, le=60)

    @field_validator("smtp_password", "smtp_password_file", mode="before")
    @classmethod
    def blank_smtp_password_source_is_unset(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def resolve_sources(self) -> MailV2WorkerSettings:
        for name in ("database_url", "settings_database_url", "encryption_key"):
            value = getattr(self, name)
            path = getattr(self, name + "_file")
            if (value is None) == (path is None):
                raise ValueError(f"Set exactly one Mail v2 {name} source")
            if path is not None:
                value = _read_secret_file(path)
                setattr(self, name, value)
            if value is None or not value.get_secret_value().strip():
                raise ValueError(f"Empty Mail v2 {name}")
            if name == "encryption_key":
                try:
                    from yoao_identity.recovery_crypto import mail_encryption_id

                    mail_encryption_id(value.get_secret_value())
                except Exception:
                    raise ValueError("Invalid Mail v2 encryption key") from None
        mail_login, mail_database = _postgres_binding(self.database_url)
        settings_login, settings_database = _postgres_binding(
            self.settings_database_url
        )
        if mail_database != settings_database:
            raise ValueError("Mail and Settings must use the same PostgreSQL target")
        if mail_login == settings_login:
            raise ValueError("Mail and Settings must use distinct PostgreSQL LOGINs")

        if self.smtp_password_file is not None:
            if self.smtp_password is not None:
                raise ValueError("Set at most one Mail v2 smtp_password source")
            self.smtp_password = _read_secret_file(
                self.smtp_password_file, max_bytes=None
            )
        if self.smtp_password is None:
            self.smtp_password = SecretStr("")

        for name in ("smtp_host", "smtp_user", "smtp_from", "host"):
            value = getattr(self, name)
            if "\r" in value or "\n" in value or "\x00" in value:
                raise ValueError(f"Invalid Mail v2 {name}")
        if not self.host.strip():
            raise ValueError("Empty Mail v2 worker host")
        return self
