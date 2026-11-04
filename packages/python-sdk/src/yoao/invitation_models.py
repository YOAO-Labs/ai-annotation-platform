"""Response models for the administrator invitation workflows."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from yoao.models import _SDKModel

InvitationStatus = Literal["pending", "accepted", "expired", "revoked", "all"]
InvitationScope = Literal["me", "all"]


class Invitation(_SDKModel):
    id: UUID
    email: str
    role: str
    group_name: str | None = None
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


class InvitationPage(_SDKModel):
    items: list[Invitation]
    total: int
    page: int
    page_size: int
    pages: int


class InvitationStats(_SDKModel):
    total: int
    pending: int
    accepted: int
    expired: int
    revoked: int


class InvitationResend(_SDKModel):
    """Resending rotates/revives the token; it does not send email."""

    invite_url: str = Field(repr=False)
    token: str = Field(repr=False)
    expires_at: datetime


class InvitationEmailSent(_SDKModel):
    invitation_id: UUID
    email: str
    invite_url: str = Field(repr=False)
    message: str
    ok: bool = True
