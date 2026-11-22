from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.schemas.user import UserOut


def _normalize_email(v: str) -> str:
    v = (v or "").strip().lower()
    if "@" not in v or len(v) < 3 or len(v) > 255:
        raise ValueError("邮箱格式不正确")
    return v


class InvitationCreate(BaseModel):
    email: str
    role: str
    group_name: str | None = Field(default=None, max_length=100)
    project_id: UUID | None = None
    project_member_role: str | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _normalize_email(v)

    @field_validator("group_name", mode="before")
    @classmethod
    def _group_name(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        normalized = v.strip()
        return normalized or None


class InvitationCreated(BaseModel):
    invite_url: str = Field(repr=False)
    token: str = Field(repr=False)
    expires_at: datetime
    project_id: UUID | None = None
    project_name: str | None = None
    project_member_role: str | None = None


class InvitationResolve(BaseModel):
    email: str
    role: str
    group_name: str | None
    project_id: UUID | None = None
    project_name: str | None = None
    project_member_role: str | None = None
    expires_at: datetime
    invited_by_name: str | None = None


class InvitationAcceptance(BaseModel):
    project_id: UUID | None = None
    project_name: str | None = None
    project_member_role: str | None = None
    next_action: str
    next_action_label: str
    responsible_person_name: str | None = None
    active_batch_count: int = 0


class AcceptInvitationRequest(BaseModel):
    token: str = Field(min_length=1, repr=False)


class AcceptInvitationResponse(BaseModel):
    user: UserOut
    acceptance: InvitationAcceptance


class RegisterRequest(BaseModel):
    token: str = Field(repr=False)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=128, repr=False)

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        from app.core.password import validate_password_strength

        errors = validate_password_strength(v)
        if errors:
            raise ValueError("; ".join(errors))
        return v


class RegisterResponse(BaseModel):
    access_token: str | None = Field(default=None, repr=False)
    token_type: str = "bearer"
    user: UserOut
    email_verification_required: bool = False
    acceptance: InvitationAcceptance | None = None


class InvitationOut(BaseModel):
    id: UUID
    email: str
    role: str
    group_name: str | None
    project_id: UUID | None = None
    project_name: str | None = None
    project_member_role: str | None = None
    status: str
    expires_at: datetime
    invited_by: UUID
    invited_by_name: str | None = None
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class InvitationResendResponse(BaseModel):
    invite_url: str = Field(repr=False)
    token: str = Field(repr=False)
    expires_at: datetime


class OpenRegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=128, repr=False)
    captcha_token: str | None = Field(default=None, repr=False)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str) -> str:
        return _normalize_email(v)

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        from app.core.password import validate_password_strength

        errors = validate_password_strength(v)
        if errors:
            raise ValueError("; ".join(errors))
        return v
