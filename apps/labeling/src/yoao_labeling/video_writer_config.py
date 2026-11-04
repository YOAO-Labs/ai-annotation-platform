"""Configuration for the private video-writer process and its API peer."""

from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _secret_value(direct: str | SecretStr | None, filename: str | None) -> str:
    value = direct.get_secret_value() if isinstance(direct, SecretStr) else direct or ""
    if value and filename:
        raise ValueError("Select a direct secret value or its file")
    if filename:
        try:
            value = Path(filename).read_text(encoding="utf-8").rstrip("\r\n")
        except (OSError, UnicodeError):
            raise ValueError("Video-writer secret file is unavailable") from None
        if not value:
            raise ValueError("Video-writer secret file is empty")
    return value


class VideoWriterSettings(BaseSettings):
    """Standalone process settings; Mask credentials remain optional until used."""

    model_config = SettingsConfigDict(
        env_prefix="LABELING_VIDEO_WRITER_",
        extra="ignore",
        populate_by_name=True,
    )

    database_url: SecretStr | None = Field(default=None, repr=False)
    database_url_file: str = ""
    control_token: SecretStr | None = Field(default=None, repr=False)
    control_token_file: str = ""
    host: str = "127.0.0.1"
    port: int = Field(default=8180, ge=1, le=65535)
    timeout_seconds: float = Field(default=90, gt=0, le=3600)
    max_concurrency: int = Field(default=8, ge=1, le=1024)

    assets_mask_content_url: str = Field(
        default="", validation_alias=AliasChoices("ASSETS_MASK_CONTENT_URL")
    )
    assets_mask_content_token: SecretStr | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("ASSETS_MASK_CONTENT_TOKEN"),
    )
    assets_mask_content_token_file: str = Field(
        default="",
        validation_alias=AliasChoices("ASSETS_MASK_CONTENT_TOKEN_FILE"),
    )
    assets_mask_content_timeout_seconds: float = Field(
        default=30,
        gt=0,
        le=3600,
        validation_alias=AliasChoices("ASSETS_MASK_CONTENT_TIMEOUT_SECONDS"),
    )

    def read_database_url(self) -> str:
        value = _secret_value(self.database_url, self.database_url_file)
        if not value:
            raise ValueError("Video-writer database DSN is required")
        return value

    def read_control_token(self) -> str:
        value = _secret_value(self.control_token, self.control_token_file)
        if not value:
            raise ValueError("Video-writer control token is required")
        return value

    def read_mask_content_credentials(self) -> tuple[str, str]:
        """Read Mask configuration lazily so non-Mask writes need no credentials."""
        token = _secret_value(
            self.assets_mask_content_token, self.assets_mask_content_token_file
        )
        if not self.assets_mask_content_url or not token:
            raise ValueError("Mask content owner is not configured")
        return self.assets_mask_content_url, token


class VideoWriterClientSettings(BaseSettings):
    """Settings for API processes that call the private Labeling owner."""

    model_config = SettingsConfigDict(
        env_prefix="LABELING_VIDEO_WRITER_",
        extra="ignore",
        populate_by_name=True,
    )

    url: str = Field(
        default="", validation_alias=AliasChoices("LABELING_VIDEO_WRITER_URL")
    )
    control_token: SecretStr | None = Field(
        default=None,
        repr=False,
        validation_alias=AliasChoices("LABELING_VIDEO_WRITER_CONTROL_TOKEN"),
    )
    control_token_file: str = Field(
        default="",
        validation_alias=AliasChoices("LABELING_VIDEO_WRITER_CONTROL_TOKEN_FILE"),
    )
    timeout_seconds: float = Field(default=90, gt=0, le=3600)

    def read_url(self) -> str:
        if not self.url:
            raise ValueError("Video-writer owner URL is required")
        return self.url

    def read_control_token(self) -> str:
        value = _secret_value(self.control_token, self.control_token_file)
        if not value:
            raise ValueError("Video-writer control token is required")
        return value
