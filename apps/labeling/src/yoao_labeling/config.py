"""Standalone Labeling class-query settings."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def secret_source(direct: str | SecretStr | None, filename: str | None) -> str:
    value = direct.get_secret_value() if isinstance(direct, SecretStr) else direct or ""
    if value and filename:
        raise ValueError("Select a direct value or its secret file")
    if filename:
        try:
            value = Path(filename).read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError):
            raise ValueError(
                "Labeling class-query secret file is unavailable"
            ) from None
        if not value:
            raise ValueError("Labeling class-query secret file is empty")
    return value


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LABELING_CLASS_QUERY_", extra="ignore"
    )

    database_url: SecretStr | None = Field(default=None, repr=False)
    database_url_file: str = ""
    control_token: SecretStr | None = Field(default=None, repr=False)
    control_token_file: str = ""
    host: str = "0.0.0.0"
    port: int = Field(default=8178, ge=1, le=65535)
    timeout_seconds: float = Field(default=30, gt=0)
    max_concurrency: int = Field(default=8, ge=1)

    def read_database_url(self) -> str:
        value = secret_source(self.database_url, self.database_url_file)
        if not value:
            raise ValueError("Labeling class-query DSN is required")
        return value

    def read_control_token(self) -> str:
        value = secret_source(self.control_token, self.control_token_file)
        if not value:
            raise ValueError("Labeling class-query control token is required")
        return value
