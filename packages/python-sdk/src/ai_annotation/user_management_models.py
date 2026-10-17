"""Typed requests and responses for account, group, and invitation management."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, field_validator, model_validator

from ai_annotation.models import PlatformRole, ProjectRole, _AAPModel


class UserRecord(_AAPModel):
    id: UUID
    email: str
    name: str
    role: str
    group_name: str | None = None
    group_id: UUID | None = None
    status: str
    is_active: bool = True
    disabled_kind: str | None = None
    disabled_at: datetime | None = None
    disabled_by: UUID | None = None
    disabled_reason: str | None = None
    email_verified_at: datetime | None = None
    last_login_at: datetime | None = None
    last_seen_at: datetime | None = None
    password_admin_reset_at: datetime | None = None
    deactivation_requested_at: datetime | None = None
    deactivation_scheduled_at: datetime | None = None
    preferences: dict[str, Any] = Field(default_factory=dict)
    avatar_ref: str | None = None
    created_at: datetime


class UserPageItem(UserRecord):
    """A visible user and the caller's account/lifecycle write flags."""

    is_managed: bool = True
    is_lifecycle_managed: bool = True


class UserPage(_AAPModel):
    items: list[UserPageItem]
    total: int
    page: int
    page_size: int
    pages: int


class UsersStats(_AAPModel):
    total: int
    online: int
    weekly_active: int


class Group(_AAPModel):
    id: UUID
    name: str
    description: str | None = None
    member_count: int = 0
    created_at: datetime


class GroupPage(_AAPModel):
    items: list[Group]
    total: int
    page: int
    page_size: int
    pages: int


class GroupCreate(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class GroupUpdate(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class InvitationCreate(_AAPModel):
    """Invitation creation; ``role`` is account role, project role is separate."""

    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=255)
    role: PlatformRole
    group_name: str | None = Field(default=None, max_length=100)
    project_id: UUID | None = None
    project_member_role: ProjectRole | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        normalized = value.strip().lower()
        if "@" not in normalized:
            raise ValueError("email must contain @")
        return normalized

    @field_validator("group_name", mode="before")
    @classmethod
    def _group_name(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def _validate_project_role(self) -> "InvitationCreate":
        if self.project_id is None and self.project_member_role is not None:
            raise ValueError("project_member_role requires project_id")
        if self.project_id is not None and self.project_member_role is None:
            raise ValueError("project_member_role is required with project_id")
        return self


class InvitationCreated(_AAPModel):
    """Creation returns a one-time invitation token and shareable URL."""

    invite_url: str = Field(repr=False)
    token: str = Field(repr=False)
    expires_at: datetime
    project_id: UUID | None = None
    project_name: str | None = None
    project_member_role: str | None = None


class BulkInviteItem(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(max_length=1000)
    role: str = Field(max_length=100)
    group_name: str | None = Field(default=None, max_length=1000)
    project_id: UUID | None = None
    project_member_role: str | None = Field(default=None, max_length=100)


class BulkInviteRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    items: list[BulkInviteItem] = Field(min_length=1, max_length=500)


class BulkInviteResultItem(_AAPModel):
    index: int
    email: str
    ok: bool
    retryable: bool = False
    invitation_id: UUID | None = None
    token: str | None = Field(default=None, repr=False)
    invite_url: str | None = Field(default=None, repr=False)
    project_id: UUID | None = None
    project_name: str | None = None
    error: str | None = None


class BulkInviteResponse(_AAPModel):
    items: list[BulkInviteResultItem]
    succeeded: int
    failed: int
    preview: bool = False


class BulkGroupAssignmentRequest(_AAPModel):
    """Assign users to a group; explicit ``group_id=None`` clears membership."""

    model_config = ConfigDict(extra="forbid")

    user_ids: list[UUID] = Field(min_length=1, max_length=500)
    group_id: UUID | None


class GroupAssignmentPreviewItem(_AAPModel):
    user_id: UUID
    email: str | None = None
    name: str | None = None
    ok: bool
    current_group_id: UUID | None = None
    current_group_name: str | None = None
    next_group_id: UUID | None = None
    next_group_name: str | None = None
    error: str | None = None


class BulkGroupAssignmentPreview(_AAPModel):
    group_id: UUID | None = None
    group_name: str | None = None
    items: list[GroupAssignmentPreviewItem]
    applicable: int
    blocked: int


class BulkGroupAssignmentResultItem(_AAPModel):
    user_id: UUID
    ok: bool
    retryable: bool = False
    error: str | None = None


class BulkGroupAssignmentResponse(_AAPModel):
    items: list[BulkGroupAssignmentResultItem]
    succeeded: int
    failed: int


class RoleImpactProject(_AAPModel):
    project_id: UUID
    project_name: str
    membership_role: str | None = None
    annotator_batch_count: int = 0
    reviewer_batch_count: int = 0
    assigned_task_count: int = 0
    review_task_count: int = 0


class RoleImpactPreview(_AAPModel):
    user_id: UUID
    email: str
    current_role: str
    requested_role: str
    can_change: bool
    blockers: list[str] = Field(default_factory=list)
    projects: list[RoleImpactProject] = Field(default_factory=list)
    assigned_batch_count: int = 0
    assigned_task_count: int = 0
    review_task_count: int = 0
    other_project_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class ChangeUserRoleRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    role: PlatformRole


class AdminResetPasswordResponse(_AAPModel):
    """Contains a temporary password; callers must handle it as a secret."""

    temp_password: str = Field(repr=False)
    message: str
    target_email: str


class DeleteUserRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    transfer_to_user_id: UUID | None = None


class OffboardingBlocker(_AAPModel):
    code: str
    message: str


class OffboardingBatchRef(_AAPModel):
    batch_id: UUID
    batch_name: str


class UserReceiverOption(_AAPModel):
    id: UUID
    name: str
    email: str
    role: str
    project_member_role: str | None = None


class OffboardingRolePreview(_AAPModel):
    present: bool = False
    batches: list[OffboardingBatchRef] = Field(default_factory=list)
    receiver_options: list[UserReceiverOption] = Field(default_factory=list)


class OffboardingProjectPreview(_AAPModel):
    project_id: UUID
    project_name: str
    roles: dict[str, OffboardingRolePreview]
    tasks: dict[str, dict[str, int]]
    locked_task_count: int = 0
    blockers: list[OffboardingBlocker] = Field(default_factory=list)


class OffboardingApiKeyPreview(_AAPModel):
    id: UUID
    name: str
    key_prefix: str
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class OffboardingPreview(_AAPModel):
    user: UserRecord
    preview_version: str
    generated_at: datetime
    projects: list[OffboardingProjectPreview] = Field(default_factory=list)
    api_keys: list[OffboardingApiKeyPreview] = Field(default_factory=list)
    blockers: list[OffboardingBlocker] = Field(default_factory=list)
    can_commit: bool = False


class OffboardingProjectRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    owner_receiver_id: UUID | None = None
    annotator_receiver_id: UUID | None = None
    reviewer_receiver_id: UUID | None = None


class OffboardingCommitRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    preview_version: str
    reason: str = Field(default="", max_length=500)
    mode: Literal["handoff", "emergency_suspend"] = "handoff"
    projects: list[OffboardingProjectRequest] = Field(default_factory=list)


class OffboardingTransferResult(_AAPModel):
    project_id: UUID
    role: str
    receiver_id: UUID | None = None
    batch_ids: list[UUID] = Field(default_factory=list)
    task_count: int = 0
    lock_count: int = 0


class OffboardingUnresolvedResult(_AAPModel):
    project_id: UUID | None = None
    role: str | None = None
    reason: str
    batch_ids: list[UUID] = Field(default_factory=list)
    task_count: int = 0
    lock_count: int = 0


class OffboardingResult(_AAPModel):
    user: UserRecord
    status: str
    mode: Literal["handoff", "emergency_suspend"]
    transfers: list[OffboardingTransferResult] = Field(default_factory=list)
    unresolved: list[OffboardingUnresolvedResult] = Field(default_factory=list)
    revoked_api_key_ids: list[UUID] = Field(default_factory=list)
    audit_id: int | None = None


class ReactivateRequest(_AAPModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=500)
