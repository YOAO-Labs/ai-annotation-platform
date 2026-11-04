from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="IDENTITY_", extra="ignore")

    database_url: SecretStr | None = None
    database_url_file: str | None = None
    actor_status_database_url: SecretStr | None = None
    actor_status_database_url_file: str | None = None
    key_database_url: SecretStr | None = None
    key_database_url_file: str | None = None
    session_database_url: SecretStr | None = None
    session_database_url_file: str | None = None
    login_redis_url: SecretStr | None = None
    login_redis_url_file: str | None = None
    account_database_url: SecretStr | None = None
    account_database_url_file: str | None = None
    presence_database_url: SecretStr | None = None
    presence_database_url_file: str | None = None
    recovery_database_url: SecretStr | None = None
    recovery_database_url_file: str | None = None
    group_database_url: SecretStr | None = None
    group_database_url_file: str | None = None
    deactivation_database_url: SecretStr | None = None
    deactivation_database_url_file: str | None = None
    deactivation_operator_database_url: SecretStr | None = None
    deactivation_operator_database_url_file: str | None = None
    invitation_database_url: SecretStr | None = None
    invitation_database_url_file: str | None = None
    directory_database_url: SecretStr | None = None
    directory_database_url_file: str | None = None
    briefs_database_url: SecretStr | None = None
    briefs_database_url_file: str | None = None
    admin_password_database_url: SecretStr | None = None
    admin_password_database_url_file: str | None = None
    platform_role_database_url: SecretStr | None = None
    platform_role_database_url_file: str | None = None
    suspension_database_url: SecretStr | None = None
    suspension_database_url_file: str | None = None
    deletion_database_url: SecretStr | None = None
    deletion_database_url_file: str | None = None
    offboarding_database_url: SecretStr | None = None
    offboarding_database_url_file: str | None = None
    recovery_mail_key: SecretStr | None = None
    recovery_mail_key_file: str | None = None
    token: SecretStr | None = None
    token_file: str | None = None
    admission_token: SecretStr | None = None
    admission_token_file: str | None = None
    signing_secret: SecretStr | None = None
    signing_secret_file: str | None = None
    host: str = "127.0.0.1"
    port: int = Field(default=8170, ge=1, le=65535)
    max_concurrency: int = Field(default=8, ge=1, le=64)
    request_timeout_seconds: float = Field(default=15, gt=0, le=120)
    connect_timeout_seconds: float = Field(default=5, gt=0, le=30)
    command_timeout_seconds: float = Field(default=10, gt=0, le=60)
    expire_minutes: int = Field(default=1440, ge=1, le=525600)
    email_verification_required: bool = False
    allow_open_registration: bool = False
    frontend_base_url: str = Field(default="http://localhost:3000", min_length=1)
    invitation_frontend_base_url: str = Field(
        default="http://localhost:5173", min_length=1
    )
    invitation_ttl_days: int = Field(default=7, ge=1, le=90)
    max_invitations_per_day: int = Field(default=30, ge=1, le=1000)
    login_captcha_threshold: int = Field(default=5, ge=0)
    login_failed_window_seconds: int = Field(default=3600, gt=0)

    @field_validator(
        "actor_status_database_url_file", "admission_token_file", mode="before"
    )
    @classmethod
    def blank_optional_secret_file_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("directory_database_url_file", mode="before")
    @classmethod
    def blank_directory_dsn_file_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("briefs_database_url_file", mode="before")
    @classmethod
    def blank_briefs_dsn_file_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("admin_password_database_url_file", mode="before")
    @classmethod
    def blank_admin_password_dsn_file_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("platform_role_database_url_file", mode="before")
    @classmethod
    def blank_platform_role_dsn_file_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("suspension_database_url_file", mode="before")
    @classmethod
    def blank_suspension_dsn_file_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("deletion_database_url_file", mode="before")
    @classmethod
    def blank_deletion_dsn_file_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("offboarding_database_url_file", mode="before")
    @classmethod
    def blank_offboarding_dsn_file_is_unset(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

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
        if self.admission_token is not None and self.admission_token_file is not None:
            raise ValueError("Set at most one admission_token source")
        admission_token = self.admission_token
        if self.admission_token_file is not None:
            try:
                with Path(self.admission_token_file).open("rb") as stream:
                    raw = stream.read(4097)
                if len(raw) > 4096:
                    raise ValueError()
                admission_token = SecretStr(raw.decode().strip())
            except (OSError, ValueError, UnicodeError):
                raise ValueError("Invalid admission_token file") from None
            self.admission_token = admission_token
        if admission_token is not None:
            normalized_admission_token = admission_token.get_secret_value()
            if (
                not normalized_admission_token
                or normalized_admission_token != normalized_admission_token.strip()
                or any(character.isspace() for character in normalized_admission_token)
                or any(
                    ord(character) < 33 or ord(character) > 126
                    for character in normalized_admission_token
                )
            ):
                raise ValueError("Invalid admission_token")
            normalized_general_token = self.token.get_secret_value().strip()
            if normalized_admission_token == normalized_general_token:
                raise ValueError("admission_token must differ from token")
        key_url = self.key_database_url
        key_path = self.key_database_url_file
        if key_url is not None and key_path is not None:
            raise ValueError("Set at most one key_database_url source")
        if key_path is not None:
            try:
                with Path(key_path).open("rb") as stream:
                    raw = stream.read(4097)
                if len(raw) > 4096:
                    raise ValueError()
                key_url = SecretStr(raw.decode().strip())
            except (OSError, ValueError, UnicodeError):
                raise ValueError("Invalid key_database_url file") from None
            self.key_database_url = key_url
        if key_url is not None and not key_url.get_secret_value().strip():
            raise ValueError("Empty key_database_url")
        for field in ("session_database_url", "login_redis_url"):
            value = getattr(self, field)
            path = getattr(self, field + "_file")
            if value is not None and path is not None:
                raise ValueError(f"Set at most one {field} source")
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
            if value is not None and not value.get_secret_value().strip():
                raise ValueError(f"Empty {field}")
        if self.session_database_url is not None and self.login_redis_url is None:
            raise ValueError("session_database_url requires login_redis_url")
        for field in (
            "account_database_url",
            "presence_database_url",
            "recovery_database_url",
            "group_database_url",
            "deactivation_database_url",
            "deactivation_operator_database_url",
            "invitation_database_url",
            "directory_database_url",
            "briefs_database_url",
            "admin_password_database_url",
            "platform_role_database_url",
            "suspension_database_url",
            "deletion_database_url",
            "offboarding_database_url",
            "actor_status_database_url",
        ):
            value = getattr(self, field)
            path = getattr(self, field + "_file")
            if value is not None and path is not None:
                raise ValueError(f"Set at most one {field} source")
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
            if value is not None and not value.get_secret_value().strip():
                raise ValueError(f"Empty {field}")
        if (
            self.recovery_mail_key is not None
            and self.recovery_mail_key_file is not None
        ):
            raise ValueError("Set at most one recovery_mail_key source")
        if self.recovery_mail_key_file is not None:
            try:
                with Path(self.recovery_mail_key_file).open("rb") as stream:
                    raw = stream.read(4097)
                if len(raw) > 4096:
                    raise ValueError()
                self.recovery_mail_key = SecretStr(raw.decode().strip())
            except (OSError, ValueError, UnicodeError):
                raise ValueError("Invalid recovery_mail_key file") from None
        if (
            self.recovery_mail_key is not None
            and not self.recovery_mail_key.get_secret_value().strip()
        ):
            raise ValueError("Empty recovery_mail_key")
        if self.recovery_database_url is not None and self.recovery_mail_key is None:
            raise ValueError("recovery_database_url requires recovery_mail_key")
        return self
