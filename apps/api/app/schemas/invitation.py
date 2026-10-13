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
    #: Account/platform role (employee | viewer | ...).  Legacy staff aliases
    #: are normalized to ``employee`` by the service.
    role: str
    group_name: str | None = Field(default=None, max_length=100)
    project_id: UUID | None = None
    #: Project responsibility for a project invitation (annotator | reviewer |
    #: viewer).  Mapped to the database ``project_role`` column.  Optional for
    #: legacy callers that put the project role in ``role``.
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
    invite_url: str
    token: str
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
    token: str = Field(min_length=1)


class AcceptInvitationResponse(BaseModel):
    user: UserOut
    acceptance: InvitationAcceptance


class RegisterRequest(BaseModel):
    token: str
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def _password_strength(cls, v: str) -> str:
        from app.core.password import validate_password_strength

        errors = validate_password_strength(v)
        if errors:
            raise ValueError("; ".join(errors))
        return v


class RegisterResponse(BaseModel):
    # v0.12.0 · 需邮箱验证时不自动登录：access_token 为 None + email_verification_required=True，
    # 前端据此显示「验证邮件已发送」而非进站。邀请注册 / 无需验证时照常返回 token。
    access_token: str | None = None
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
    status: str  # pending | accepted | expired | revoked
    expires_at: datetime
    invited_by: UUID
    invited_by_name: str | None = None
    accepted_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class InvitationResendResponse(BaseModel):
    invite_url: str
    token: str
    expires_at: datetime


class OpenRegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    name: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=8, max_length=128)
    # v0.8.7 · Cloudflare Turnstile token；TURNSTILE_ENABLED=False 时忽略，
    # production 启用后必填且必须通过 siteverify。
    captcha_token: str | None = None

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
