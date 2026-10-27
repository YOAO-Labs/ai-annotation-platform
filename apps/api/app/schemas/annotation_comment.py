from typing import Literal

from pydantic import BaseModel, Field, PositiveInt, model_validator
from uuid import UUID
from datetime import datetime

from app.schemas._jsonb_types import (
    ATTACHMENT_KEY_PREFIX,
    Attachment,
    CanvasDrawing,
    CommentAnchor,
    Mention,
)
from app.schemas.annotation_feedback import AnnotationFeedbackOut
from app.schemas.discussion_actions import DiscussionActions


# 兼容旧导入路径
__all__ = [
    "ATTACHMENT_KEY_PREFIX",
    "Mention",
    "Attachment",
    "CanvasDrawing",
    "CommentAnchor",
    "AnnotationCommentCreate",
    "AnnotationCommentUpdate",
    "AnnotationCommentOut",
    "AnnotationCommentListPage",
    "AnnotationCommentCountsOut",
    "TaskDiscussionItem",
    "TaskDiscussionPage",
    "CommentAttachmentUploadInitRequest",
    "CommentAttachmentUploadInitResponse",
]


class AnnotationCommentCreate(BaseModel):
    # body 可空：允许「只画批注 / 只传附件」的评论（前端 CommentInput 也按此放行）。
    # 但整条评论必须至少有 body / 附件 / 画布批注之一，见 _require_content。
    body: str = Field(default="", max_length=4000)
    mentions: list[Mention] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    canvas_drawing: CanvasDrawing | None = None
    anchor: CommentAnchor | None = None

    @model_validator(mode="after")
    def _require_content(self) -> "AnnotationCommentCreate":
        has_drawing = (
            self.canvas_drawing is not None and len(self.canvas_drawing.shapes) > 0
        )
        if not self.body.strip() and not self.attachments and not has_drawing:
            raise ValueError("评论需至少包含正文、附件或画布批注之一")
        return self


class AnnotationCommentUpdate(BaseModel):
    # body 与 create 口径对齐：字段级不再强制 min_length=1（None=不改、""=清空正文）。
    # 「清空后整条评论不能为空」的不变式由 patch_comment 端点结合现有附件/画布批注校验。
    body: str | None = Field(default=None, max_length=4000)
    is_resolved: bool | None = None


class AnnotationCommentOut(BaseModel):
    id: UUID
    annotation_id: UUID
    project_id: UUID | None = None
    author_id: UUID
    author_name: str | None = None
    body: str
    is_resolved: bool
    is_active: bool
    mentions: list[Mention] = []
    attachments: list[Attachment] = []
    canvas_drawing: CanvasDrawing | None = None
    anchor: CommentAnchor | None = None
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class AnnotationCommentListPage(BaseModel):
    """v0.8.8 · keyset 分页响应。

    cursor 编码 ``<created_at_iso>__<id>``；前端按返回的 ``next_cursor``
    继续拉「更早评论」。空数组 + ``next_cursor=None`` 即末尾。
    """

    items: list[AnnotationCommentOut]
    next_cursor: str | None = None


class AnnotationCommentCountsOut(BaseModel):
    """Exact active annotation-comment counts for a visible task."""

    counts: dict[str, PositiveInt] = Field(default_factory=dict)


class TaskDiscussionItem(BaseModel):
    """One row in the authoritative, mixed task discussion feed.

    ``data`` deliberately retains the source endpoint's complete response shape.  The
    source discriminator lives beside it because annotation comments and feedbacks can
    have the same UUID; consumers must use ``(source, data.id)`` as the identity.
    """

    source: Literal["annotation_comment", "feedback"]
    data: AnnotationCommentOut | AnnotationFeedbackOut
    actions: DiscussionActions


class TaskDiscussionPage(BaseModel):
    """Keyset-paged task discussion read model."""

    items: list[TaskDiscussionItem]
    next_cursor: str | None = None
    total: int = Field(ge=0)


class CommentAttachmentUploadInitRequest(BaseModel):
    """Compatibility request for the retired presigned upload initializer."""

    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(
        default="application/octet-stream", min_length=1, max_length=128
    )


class CommentAttachmentUploadInitResponse(BaseModel):
    storage_key: str
    upload_url: str
    expires_in: int
