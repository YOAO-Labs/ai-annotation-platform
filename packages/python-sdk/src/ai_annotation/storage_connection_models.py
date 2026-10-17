"""Typed, secret-safe models for administrator storage connections."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ai_annotation.models import _AAPModel

StorageKind = Literal["s3", "sftp"]
StorageScope = Literal["global", "owner"]


class _RequestModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        allow_inf_nan=False,
        protected_namespaces=("model_validate", "model_dump"),
    )


class S3StorageConfig(_RequestModel):
    endpoint: str
    bucket: str
    region: str | None = None
    base_prefix: str | None = None
    use_ssl: bool | None = None


class SFTPStorageConfig(_RequestModel):
    host: str
    username: str
    port: int | None = None
    base_path: str | None = None
    auth_type: Literal["password", "key"] | None = None


StorageConfig = S3StorageConfig | SFTPStorageConfig


class S3StorageSecret(_RequestModel):
    access_key: str = Field(min_length=1, repr=False)
    secret_key: str = Field(min_length=1, repr=False)


class SFTPStorageSecret(_RequestModel):
    password: str | None = Field(default=None, min_length=1, repr=False)
    private_key: str | None = Field(default=None, min_length=1, repr=False)
    passphrase: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def has_authentication(self) -> SFTPStorageSecret:
        if bool(self.password) == bool(self.private_key):
            raise ValueError("SFTP requires exactly one of password or private_key")
        return self


StorageSecret = S3StorageSecret | SFTPStorageSecret


class StorageConnectionCreate(_RequestModel):
    name: str = Field(min_length=1, max_length=255)
    kind: StorageKind
    config: StorageConfig = Field(repr=False)
    secret: StorageSecret = Field(repr=False)
    scope: StorageScope = "owner"
    # The API accepts this historical field but the service always stores None.
    project_id: UUID | None = None

    @model_validator(mode="after")
    def configuration_matches_kind(self) -> StorageConnectionCreate:
        expected_config = S3StorageConfig if self.kind == "s3" else SFTPStorageConfig
        expected_secret = S3StorageSecret if self.kind == "s3" else SFTPStorageSecret
        if not isinstance(self.config, expected_config) or not isinstance(
            self.secret, expected_secret
        ):
            raise ValueError("config and secret must match kind")
        if self.kind == "sftp" and self.config.auth_type is not None:
            actual_auth = "key" if self.secret.private_key else "password"
            if self.config.auth_type != actual_auth:
                raise ValueError("SFTP auth_type must match the supplied credential")
        return self


class StorageConnectionUpdate(_RequestModel):
    """Partial update; a non-null secret rotates the complete credential set."""

    name: str | None = Field(default=None, min_length=1, max_length=255)
    config: StorageConfig | None = Field(default=None, repr=False)
    secret: StorageSecret | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def configuration_matches_secret(self) -> StorageConnectionUpdate:
        if (
            isinstance(self.config, SFTPStorageConfig)
            and isinstance(self.secret, SFTPStorageSecret)
            and self.config.auth_type is not None
        ):
            actual_auth = "key" if self.secret.private_key else "password"
            if self.config.auth_type != actual_auth:
                raise ValueError("SFTP auth_type must match the supplied credential")
        return self


class StorageConnection(_AAPModel):
    id: UUID
    name: str
    kind: StorageKind
    config: dict[str, Any] = Field(default_factory=dict)
    scope: StorageScope
    project_id: UUID | None = None
    secret_set: bool
    created_by: UUID | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def strip_unexpected_secret_fields(cls, value: Any) -> Any:
        if isinstance(value, dict):
            value = dict(value)
            value.pop("secret", None)
            value.pop("secret_enc", None)
        return value


class ConnectorAllowlistUpdate(_RequestModel):
    entries: list[Annotated[str, Field(max_length=253)]] = Field(max_length=256)


class ConnectorAllowlist(_AAPModel):
    entries: list[str]
    source: Literal["database", "environment"]


class DeploymentSFTPStoragePreset(_AAPModel):
    enabled: bool
    host: str | None = None
    port: int = 22


class StorageConnectionTestResult(_AAPModel):
    """HTTP 200 with ``ok=False`` is a normal negative connectivity result."""

    ok: bool
    message: str
    sample_count: int | None = None
