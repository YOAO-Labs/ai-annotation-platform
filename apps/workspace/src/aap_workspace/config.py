"""Standalone Workspace owner settings, independent of the API environment."""

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
            raise ValueError("Workspace secret file is unavailable") from None
    return value


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WORKSPACE_", extra="ignore")

    membership_database_url: SecretStr | None = Field(default=None, repr=False)
    membership_database_url_file: str = ""
    owner_transfer_database_url: SecretStr | None = Field(default=None, repr=False)
    owner_transfer_database_url_file: str = ""
    configuration_database_url: SecretStr | None = Field(default=None, repr=False)
    configuration_database_url_file: str = ""
    template_query_database_url: SecretStr | None = Field(default=None, repr=False)
    template_query_database_url_file: str = ""
    configuration_setup_timeout_seconds: float = Field(default=10, gt=0)
    configuration_video_segment_size_frames: int = Field(default=18000, ge=1)
    configuration_video_tracker_window_size_frames: int = Field(default=300, ge=1)
    configuration_video_tracker_sam3_window_size_frames: int = Field(default=16, ge=1)
    membership_control_token: SecretStr | None = Field(default=None, repr=False)
    membership_control_token_file: str = ""
    host: str = "0.0.0.0"
    port: int = Field(default=8172, ge=1, le=65535)
    timeout_seconds: float = Field(default=30, gt=0)
    max_concurrency: int = Field(default=8, ge=1)

    def database_url(self) -> str:
        value = secret_source(
            self.membership_database_url, self.membership_database_url_file
        )
        if not value:
            raise ValueError("Workspace membership DSN is required")
        return value

    def transfer_database_url(self) -> str:
        return secret_source(
            self.owner_transfer_database_url, self.owner_transfer_database_url_file
        )

    def config_database_url(self) -> str:
        return secret_source(
            self.configuration_database_url, self.configuration_database_url_file
        )

    def query_database_url(self) -> str:
        return secret_source(
            self.template_query_database_url, self.template_query_database_url_file
        )

    def control_token(self) -> str:
        value = secret_source(
            self.membership_control_token, self.membership_control_token_file
        )
        if not value:
            raise ValueError("Workspace membership control token is required")
        return value
