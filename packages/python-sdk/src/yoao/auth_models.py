"""Typed request and response models for account authentication operations."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _AuthRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        protected_namespaces=("model_validate", "model_dump"),
        hide_input_in_errors=True,
    )


class _AuthResponse(BaseModel):
    model_config = ConfigDict(
        extra="allow",
        protected_namespaces=("model_validate", "model_dump"),
        hide_input_in_errors=True,
    )


def _validate_password_strength(value: str) -> str:
    if (
        not re.search(r"[A-Z]", value)
        or not re.search(r"[a-z]", value)
        or not re.search(r"\d", value)
    ):
        raise ValueError("password must include uppercase, lowercase, and a digit")
    return value


class _PasswordRequest(_AuthRequest):
    @field_validator("password", "new_password", check_fields=False)
    @classmethod
    def strong_password(cls, value: str) -> str:
        return _validate_password_strength(value)


class AuthForgotPasswordRequest(_AuthRequest):
    email: str = Field(min_length=3, max_length=255)
    captcha_token: str | None = Field(default=None, repr=False)


class AuthAcceptInvitationRequest(_AuthRequest):
    token: str = Field(min_length=1, repr=False)


class AuthLoginRequest(_AuthRequest):
    email: str
    password: str = Field(repr=False)
    captcha_token: str | None = Field(default=None, repr=False)


class AuthRegisterRequest(_PasswordRequest):
    token: str = Field(repr=False)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=128, repr=False)


class AuthOpenRegisterRequest(_PasswordRequest):
    email: str = Field(min_length=3, max_length=255)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=128, repr=False)
    captcha_token: str | None = Field(default=None, repr=False)

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        value = value.strip().lower()
        if "@" not in value or not 3 <= len(value) <= 255:
            raise ValueError("email must contain @ and have 3–255 characters")
        return value


class AuthResetPasswordRequest(_PasswordRequest):
    token: str = Field(min_length=1, repr=False)
    new_password: str = Field(min_length=8, max_length=128, repr=False)


class AuthSendVerificationEmailRequest(_AuthRequest):
    email: str = Field(min_length=3, max_length=255)


class AuthVerifyEmailRequest(_AuthRequest):
    token: str = Field(min_length=1, repr=False)


class AuthToken(_AuthResponse):
    access_token: str = Field(repr=False)
    token_type: str = "bearer"


class AuthMessage(_AuthResponse):
    message: str


class AuthRegistrationStatus(_AuthResponse):
    open_registration_enabled: bool


class AuthUser(_AuthResponse):
    id: UUID
    email: str
    name: str
    role: str


class AuthInvitationAcceptance(_AuthResponse):
    project_id: UUID | None = None
    project_name: str | None = None
    project_member_role: str | None = None
    next_action: str
    next_action_label: str
    responsible_person_name: str | None = None
    active_batch_count: int = 0


class AuthInvitationResolve(_AuthResponse):
    email: str
    role: str
    group_name: str | None
    project_id: UUID | None = None
    project_name: str | None = None
    project_member_role: str | None = None
    expires_at: datetime
    invited_by_name: str | None = None


class AuthAcceptInvitationResponse(_AuthResponse):
    user: AuthUser
    acceptance: AuthInvitationAcceptance


class AuthRegisterResponse(_AuthResponse):
    access_token: str | None = Field(default=None, repr=False)
    token_type: str = "bearer"
    user: AuthUser
    email_verification_required: bool = False
    acceptance: AuthInvitationAcceptance | None = None


def _auth_body(model: _AuthRequest) -> dict[str, Any]:
    """Serialize only fields accepted by the server, omitting absent optionals."""
    return model.model_dump(mode="json", by_alias=True, exclude_none=True)
