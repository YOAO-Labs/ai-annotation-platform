from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


BUG_ATTACHMENT_KEY_PREFIX = "bug-report-attachments/"
BUG_ATTACHMENT_LEGACY_PREFIX = "bug-screenshots/"
BUG_ATTACHMENT_LEGACY_PATTERN = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/"
)
BUG_ATTACHMENT_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}


class BugAttachment(BaseModel):
    storage_key: str = Field(alias="storageKey", min_length=1, max_length=512)
    file_name: str = Field(alias="fileName", min_length=1, max_length=255)
    mime_type: str = Field(alias="mimeType", min_length=1, max_length=128)
    size: int = Field(ge=0, le=10 * 1024 * 1024)

    model_config = ConfigDict(populate_by_name=True)

    @field_validator("storage_key")
    @classmethod
    def _validate_storage_key(cls, v: str) -> str:
        import re

        if (
            v.startswith(BUG_ATTACHMENT_KEY_PREFIX)
            or v.startswith(BUG_ATTACHMENT_LEGACY_PREFIX)
            or re.match(BUG_ATTACHMENT_LEGACY_PATTERN, v)
        ):
            return v
        raise ValueError(
            f"attachments[].storageKey 必须以 {BUG_ATTACHMENT_KEY_PREFIX!r} 开头"
        )

    @field_validator("mime_type")
    @classmethod
    def _validate_mime_type(cls, v: str) -> str:
        if v not in BUG_ATTACHMENT_MIME_TYPES:
            raise ValueError("BUG 截图仅支持 PNG / JPEG / WebP")
        return v


class BugReportCreate(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=20000)
    severity: str = Field(default="medium", pattern="^(low|medium|high|critical)$")
    route: str = ""
    browser_ua: str | None = None
    viewport: str | None = None
    project_id: UUID | None = None
    task_id: UUID | None = None
    recent_api_calls: list[dict] | None = Field(default=None, max_length=100)
    recent_console_errors: list[dict] | None = Field(default=None, max_length=100)
    screenshot_url: str | None = None
    attachments: list[BugAttachment] = Field(default_factory=list, max_length=5)


class BugReportUpdate(BaseModel):
    status: str | None = Field(
        default=None, pattern="^(new|triaged|in_progress|fixed|wont_fix|duplicate)$"
    )
    severity: str | None = Field(default=None, pattern="^(low|medium|high|critical)$")
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = Field(default=None, min_length=1, max_length=20000)
    duplicate_of_id: UUID | None = None
    assigned_to_id: UUID | None = None
    fixed_in_version: str | None = None
    resolution: str | None = None

    @field_validator("title", "description", "severity")
    @classmethod
    def non_nullable_content(cls, value):
        if value is None:
            raise ValueError("title, description and severity cannot be null")
        return value


class BugCommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=10000)


class BugCommentOut(BaseModel):
    id: UUID
    bug_report_id: UUID
    author_id: UUID
    author_name: str = ""
    author_role: str = ""
    body: str
    created_at: datetime

    class Config:
        from_attributes = True


class BugReportOut(BaseModel):
    id: UUID
    display_id: str
    reporter_id: UUID
    route: str
    user_role: str
    project_id: UUID | None = None
    task_id: UUID | None = None
    title: str
    description: str
    severity: str
    status: str
    duplicate_of_id: UUID | None = None
    browser_ua: str | None = None
    viewport: str | None = None
    recent_api_calls: list[dict] | None = None
    recent_console_errors: list[dict] | None = None
    screenshot_url: str | None = None
    attachments: list[BugAttachment] = Field(default_factory=list)
    resolution: str | None = None
    fixed_in_version: str | None = None
    assigned_to_id: UUID | None = None
    created_at: datetime
    triaged_at: datetime | None = None
    fixed_at: datetime | None = None
    reopen_count: int = 0
    last_reopened_at: datetime | None = None

    class Config:
        from_attributes = True


class BugReportDetail(BugReportOut):
    comments: list[BugCommentOut] = Field(default_factory=list)


class BugReportList(BaseModel):
    items: list[BugReportOut]
    total: int
