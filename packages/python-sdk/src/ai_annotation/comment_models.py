"""Typed request and response models for annotation discussion endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ai_annotation.models import DiscussionActions as DiscussionActions


class _CommentModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class _CommentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CommentMention(_CommentModel):
    user_id: UUID = Field(alias="userId")
    display_name: str = Field(alias="displayName", min_length=1, max_length=120)
    offset: int = Field(ge=0)
    length: int = Field(ge=1)


class CommentMentionInput(_CommentRequest):
    user_id: UUID = Field(alias="userId")
    display_name: str = Field(alias="displayName", min_length=1, max_length=120)
    offset: int = Field(ge=0)
    length: int = Field(ge=1)


class CommentAttachment(_CommentModel):
    storage_key: str = Field(alias="storageKey", min_length=1, max_length=512)
    file_name: str = Field(alias="fileName", min_length=1, max_length=255)
    mime_type: str = Field(alias="mimeType", min_length=1, max_length=128)
    size: int = Field(ge=0)


class CommentAttachmentInput(_CommentRequest):
    storage_key: str = Field(alias="storageKey", min_length=1, max_length=512)
    file_name: str = Field(alias="fileName", min_length=1, max_length=255)
    mime_type: str = Field(alias="mimeType", min_length=1, max_length=128)
    size: int = Field(ge=0)

    @field_validator("storage_key")
    @classmethod
    def _validate_key_prefix(cls, value: str) -> str:
        if not value.startswith("comment-attachments/"):
            raise ValueError("comment attachment storage key has an invalid prefix")
        return value


class CommentCanvasShape(_CommentRequest):
    type: Literal["line", "arrow", "rect", "ellipse"]
    points: list[float] = Field(min_length=2)
    stroke: str | None = None
    id: str | None = None
    started_at: float | None = None
    ended_at: float | None = None


class CommentCanvasDrawing(_CommentRequest):
    shapes: list[CommentCanvasShape] = Field(default_factory=list)


class CommentAnchor(_CommentRequest):
    kind: Literal["video_frame"]
    frame_index: int = Field(ge=0, alias="frameIndex")
    track_id: str | None = Field(default=None, alias="trackId", max_length=120)
    source: Literal["manual", "prediction", "interpolated", "legacy"] | None = None


class CommentAnchorOut(_CommentModel):
    kind: Literal["video_frame"]
    frame_index: int = Field(ge=0, alias="frameIndex")
    track_id: str | None = Field(default=None, alias="trackId", max_length=120)
    source: Literal["manual", "prediction", "interpolated", "legacy"] | None = None


class AnnotationCommentCreate(_CommentRequest):
    body: str = Field(default="", max_length=4000)
    mentions: list[CommentMentionInput] = Field(default_factory=list)
    attachments: list[CommentAttachmentInput] = Field(default_factory=list)
    canvas_drawing: CommentCanvasDrawing | None = None
    anchor: CommentAnchor | None = None

    @model_validator(mode="after")
    def _require_content(self) -> AnnotationCommentCreate:
        has_drawing = self.canvas_drawing is not None and bool(
            self.canvas_drawing.shapes
        )
        if not self.body.strip() and not self.attachments and not has_drawing:
            raise ValueError(
                "comment needs body text, an attachment, or canvas drawing"
            )
        return self


class AnnotationCommentUpdate(_CommentRequest):
    # The API treats null as "leave unchanged"; an explicit empty body can clear text
    # only when the existing comment still contains an attachment or drawing.
    body: str | None = Field(default=None, max_length=4000)
    is_resolved: bool | None = None


class AnnotationComment(_CommentModel):
    id: UUID
    annotation_id: UUID
    project_id: UUID | None = None
    author_id: UUID
    author_name: str | None = None
    body: str
    is_resolved: bool
    is_active: bool
    mentions: list[CommentMention] = Field(default_factory=list)
    attachments: list[CommentAttachment] = Field(default_factory=list)
    canvas_drawing: CommentCanvasDrawing | None = None
    anchor: CommentAnchorOut | None = None
    created_at: datetime
    updated_at: datetime | None = None


class AnnotationCommentPage(_CommentModel):
    items: list[AnnotationComment]
    next_cursor: str | None = None


class DiscussionFeedback(_CommentModel):
    """Feedback shape as embedded in the mixed task discussion feed."""

    id: UUID
    kind: Literal["issue", "comment", "reject", "bug"]
    anchor_type: Literal["project", "task", "annotation", "pixel", "point_cloud"]
    project_id: UUID
    task_id: UUID | None = None
    annotation_id: UUID | None = None
    anchor_position: dict[str, Any] | None = None
    status: Literal["open", "resolved", "wont_fix"]
    severity: Literal["info", "warn", "blocker"] | None = None
    title: str | None = None
    body: str
    author_id: UUID
    author_name: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    mentions: list[CommentMention] = Field(default_factory=list)
    canvas_drawing: CommentCanvasDrawing | None = None
    thread_parent_id: UUID | None = None
    is_active: bool
    resolved_at: datetime | None = None
    resolved_by_id: UUID | None = None
    created_at: datetime
    updated_at: datetime | None = None
    actions: DiscussionActions = Field(default_factory=DiscussionActions)


class TaskDiscussionItem(_CommentModel):
    source: Literal["annotation_comment", "feedback"]
    data: AnnotationComment | DiscussionFeedback
    actions: DiscussionActions


class TaskDiscussionPage(_CommentModel):
    items: list[TaskDiscussionItem]
    next_cursor: str | None = None
    total: int = Field(ge=0)


class AnnotationCommentCounts(_CommentModel):
    counts: dict[str, int] = Field(default_factory=dict)


class CommentAttachmentUploadRequest(_CommentRequest):
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(
        default="application/octet-stream", min_length=1, max_length=128
    )


class CommentAttachmentUpload(_CommentModel):
    storage_key: str
    upload_url: str = Field(repr=False)
    expires_in: int


class CommentAttachmentDownloadURL(_CommentModel):
    download_url: str = Field(repr=False)


__all__ = [
    "AnnotationComment",
    "AnnotationCommentCounts",
    "AnnotationCommentCreate",
    "AnnotationCommentPage",
    "AnnotationCommentUpdate",
    "CommentAnchor",
    "CommentAnchorOut",
    "CommentAttachment",
    "CommentAttachmentInput",
    "CommentAttachmentDownloadURL",
    "CommentAttachmentUpload",
    "CommentAttachmentUploadRequest",
    "CommentCanvasDrawing",
    "CommentCanvasShape",
    "CommentMention",
    "CommentMentionInput",
    "DiscussionActions",
    "DiscussionFeedback",
    "TaskDiscussionItem",
    "TaskDiscussionPage",
]
