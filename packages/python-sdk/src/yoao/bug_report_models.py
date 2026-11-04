"""Typed request and response models for bug-report operations."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _BugReportResponse(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class _BugReportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


BugSeverity = Literal["low", "medium", "high", "critical"]
BugStatus = Literal["new", "triaged", "in_progress", "fixed", "wont_fix", "duplicate"]
BugAttachmentMime = Literal["image/png", "image/jpeg", "image/webp"]


class BugAttachmentInput(_BugReportRequest):
    storage_key: str = Field(alias="storageKey", min_length=1, max_length=512)
    file_name: str = Field(alias="fileName", min_length=1, max_length=255)
    mime_type: BugAttachmentMime = Field(alias="mimeType")
    size: int = Field(ge=0, le=10 * 1024 * 1024)

    @field_validator("storage_key")
    @classmethod
    def _validate_storage_key(cls, value: str) -> str:
        if value.startswith(
            ("bug-report-attachments/", "bug-screenshots/")
        ) or re.match(
            r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/",
            value,
        ):
            return value
        raise ValueError("attachment storage key has an unsupported prefix")


class BugAttachment(_BugReportResponse):
    storage_key: str = Field(alias="storageKey", min_length=1, max_length=512)
    file_name: str = Field(alias="fileName", min_length=1, max_length=255)
    mime_type: BugAttachmentMime = Field(alias="mimeType")
    size: int = Field(ge=0, le=10 * 1024 * 1024)


class BugReportCreate(_BugReportRequest):
    title: str = Field(min_length=1, max_length=500)
    description: str = Field(min_length=1, max_length=20000)
    severity: BugSeverity = "medium"
    route: str = ""
    browser_ua: str | None = None
    viewport: str | None = None
    # These IDs are diagnostic context; authorization always comes from the API.
    project_id: UUID | None = None
    task_id: UUID | None = None
    recent_api_calls: list[dict[str, Any]] | None = Field(default=None, max_length=100)
    recent_console_errors: list[dict[str, Any]] | None = Field(
        default=None, max_length=100
    )
    screenshot_url: str | None = None
    attachments: list[BugAttachmentInput] = Field(default_factory=list, max_length=5)


class BugReportUpdate(_BugReportRequest):
    """PATCH fields; reporters may edit title/description/severity, managers all fields."""

    status: BugStatus | None = None
    severity: BugSeverity | None = None
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


class BugCommentCreate(_BugReportRequest):
    body: str = Field(min_length=1, max_length=10000)


class BugComment(_BugReportResponse):
    id: UUID
    bug_report_id: UUID
    author_id: UUID
    author_name: str = ""
    author_role: str = ""
    body: str
    created_at: datetime


class BugReport(_BugReportResponse):
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
    recent_api_calls: list[dict[str, Any]] | None = None
    recent_console_errors: list[dict[str, Any]] | None = None
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


class BugReportDetail(BugReport):
    comments: list[BugComment] = Field(default_factory=list)


class BugReportPage(_BugReportResponse):
    items: list[BugReport]
    total: int


class BugScreenshotUploadRequest(_BugReportRequest):
    file_name: str = Field(default="screenshot.png", min_length=1, max_length=200)
    content_type: BugAttachmentMime = "image/png"


class BugScreenshotUpload(_BugReportResponse):
    storage_key: str
    upload_url: str = Field(repr=False)
    expires_in: int = 900


class BugScreenshot(BugAttachmentInput):
    """Local-file metadata returned after the upload helper completes."""


class BugClusterItem(_BugReportResponse):
    report_id: UUID
    similar_ids: list[UUID]


class BugReportClusters(_BugReportResponse):
    clusters: list[BugClusterItem]


__all__ = [
    "BugAttachment",
    "BugAttachmentInput",
    "BugComment",
    "BugCommentCreate",
    "BugReport",
    "BugReportClusters",
    "BugReportCreate",
    "BugReportDetail",
    "BugReportPage",
    "BugReportUpdate",
    "BugScreenshot",
    "BugScreenshotUpload",
    "BugScreenshotUploadRequest",
    "BugSeverity",
    "BugStatus",
]
