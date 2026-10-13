"""Schemas for the admin management surfaces introduced in Phase E.

These contracts intentionally live beside the existing resource schemas.  The
legacy list endpoints keep their array responses, while the management
endpoints use explicit paginated envelopes and per-item operation results.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.group import GroupOut
from app.schemas.invitation import InvitationOut
from app.schemas.user import UserOut


class UserPageItem(UserOut):
    """User row plus request-relative capability flags.

    ``is_managed`` tells the frontend whether the current actor may run
    account-management actions on the row (role change, password reset,
    group assignment).  Read visibility can be wider than the manage scope
    (project admins see enabled annotators/reviewers incl. unassigned ones,
    and read-only super admins), so visible rows are not necessarily
    manageable.

    ``is_lifecycle_managed`` further gates the work-handover writes
    (offboarding, deactivate, delete, reactivate): project admins may only
    hand over users whose derived projects (memberships, batches, tasks,
    locks, owned projects) all sit inside projects they own.  A foreign
    enabled annotator therefore lists ``is_managed=true`` but
    ``is_lifecycle_managed=false``; lifecycle buttons must be hidden so
    they cannot surface actions the API rejects with 403.
    """

    is_managed: bool = True
    is_lifecycle_managed: bool = True


class UserPage(BaseModel):
    items: list[UserPageItem]
    total: int
    page: int
    page_size: int
    pages: int


class UserStats(BaseModel):
    total: int
    online: int
    weekly_active: int


class GroupPage(BaseModel):
    items: list[GroupOut]
    total: int
    page: int
    page_size: int
    pages: int


class InvitationPage(BaseModel):
    items: list[InvitationOut]
    total: int
    page: int
    page_size: int
    pages: int


class InvitationStats(BaseModel):
    total: int
    pending: int
    accepted: int
    expired: int
    revoked: int


class InvitationSendEmailResponse(BaseModel):
    ok: bool = True
    invitation_id: UUID
    email: str
    invite_url: str
    message: str


class BulkInviteItem(BaseModel):
    # Validate business fields per item, so one invalid email does not discard
    # every otherwise valid invitation in the same request.
    email: str = Field(max_length=1000)
    role: str = Field(max_length=100)
    group_name: str | None = Field(default=None, max_length=1000)
    project_id: UUID | None = None
    project_member_role: str | None = Field(default=None, max_length=100)


class BulkInviteRequest(BaseModel):
    items: list[BulkInviteItem] = Field(min_length=1, max_length=500)


class BulkInviteResultItem(BaseModel):
    index: int
    email: str
    ok: bool
    retryable: bool = False
    invitation_id: UUID | None = None
    token: str | None = None
    invite_url: str | None = None
    project_id: UUID | None = None
    project_name: str | None = None
    error: str | None = None


class BulkInviteResponse(BaseModel):
    items: list[BulkInviteResultItem]
    succeeded: int
    failed: int
    preview: bool = False


class BulkGroupAssignmentRequest(BaseModel):
    user_ids: list[UUID] = Field(min_length=1, max_length=500)
    group_id: UUID | None = None


class GroupAssignmentPreviewItem(BaseModel):
    user_id: UUID
    email: str | None = None
    name: str | None = None
    ok: bool
    current_group_id: UUID | None = None
    current_group_name: str | None = None
    next_group_id: UUID | None = None
    next_group_name: str | None = None
    error: str | None = None


class BulkGroupAssignmentPreview(BaseModel):
    group_id: UUID | None = None
    group_name: str | None = None
    items: list[GroupAssignmentPreviewItem]
    applicable: int
    blocked: int


class BulkGroupAssignmentResultItem(BaseModel):
    user_id: UUID
    ok: bool
    retryable: bool = False
    error: str | None = None


class BulkGroupAssignmentResponse(BaseModel):
    items: list[BulkGroupAssignmentResultItem]
    succeeded: int
    failed: int


class BatchDistributionPreviewItem(BaseModel):
    batch_id: UUID
    display_id: str
    name: str
    status: str
    task_count: int = 0
    before_annotator_id: UUID | None = None
    after_annotator_id: UUID | None = None
    before_reviewer_id: UUID | None = None
    after_reviewer_id: UUID | None = None
    will_change: bool
    skipped_reason: str | None = None


class BatchDistributionRecipient(BaseModel):
    user_id: UUID
    role: Literal["annotator", "reviewer"]
    new_task_count: int
    existing_backlog_count: int


class BatchDistributionPreview(BaseModel):
    project_id: UUID
    only_unassigned: bool
    total_batches: int
    candidate_batches: int
    changed_batches: int
    skipped_batches: int
    items: list[BatchDistributionPreviewItem]
    recipient_summary: list[BatchDistributionRecipient]
    preview_version: str


class RoleImpactProject(BaseModel):
    project_id: UUID
    project_name: str
    membership_role: str | None = None
    annotator_batch_count: int = 0
    reviewer_batch_count: int = 0
    assigned_task_count: int = 0
    review_task_count: int = 0


class RoleImpactPreview(BaseModel):
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


class ManagementStatusFilter(BaseModel):
    status: Literal["active", "inactive", "all"] = "active"
