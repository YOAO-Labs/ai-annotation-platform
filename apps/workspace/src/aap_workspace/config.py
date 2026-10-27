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
        if not value:
            raise ValueError("Workspace secret file is empty")
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
    project_query_database_url: SecretStr | None = Field(default=None, repr=False)
    project_query_database_url_file: str = ""
    directory_query_database_url: SecretStr | None = Field(default=None, repr=False)
    directory_query_database_url_file: str = ""
    project_query_identity_url: str = Field(default="", repr=False)
    project_query_identity_token: SecretStr | None = Field(default=None, repr=False)
    project_query_identity_token_file: str = ""
    project_query_identity_briefs_database_url: SecretStr | None = Field(
        default=None, repr=False
    )
    project_query_identity_briefs_database_url_file: str = ""
    project_query_identity_timeout_seconds: float = Field(default=2, gt=0, le=3600)
    configuration_setup_timeout_seconds: float = Field(default=10, gt=0)
    inference_probe_url: str = Field(default="", repr=False)
    inference_probe_token: SecretStr | None = Field(default=None, repr=False)
    inference_probe_token_file: str = ""
    inference_probe_timeout_seconds: float = Field(default=30, gt=0, le=3600)
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

    def project_database_url(self) -> str:
        return secret_source(
            self.project_query_database_url, self.project_query_database_url_file
        )

    def directory_database_url(self) -> str:
        return secret_source(
            self.directory_query_database_url, self.directory_query_database_url_file
        )

    def inference_probe_client(self):
        try:
            token = secret_source(
                self.inference_probe_token, self.inference_probe_token_file
            )
        except ValueError:
            raise ValueError(
                "Workspace Inference probe configuration is invalid"
            ) from None
        if not self.inference_probe_url and not token:
            return None
        if not self.inference_probe_url or not token:
            raise ValueError(
                "Workspace Inference probe URL and token are required together"
            )
        from aap_inference_contract.client import BackendProbeHttpClient

        return BackendProbeHttpClient(
            self.inference_probe_url,
            token,
            timeout_seconds=self.inference_probe_timeout_seconds,
        )

    def control_token(self) -> str:
        value = secret_source(
            self.membership_control_token, self.membership_control_token_file
        )
        if not value:
            raise ValueError("Workspace membership control token is required")
        return value
