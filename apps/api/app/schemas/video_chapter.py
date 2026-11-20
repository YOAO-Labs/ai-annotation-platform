from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

# 章节的 frame_step / source 不新增数据库列, 而是约定存进
# chapter_metadata (JSONB) 的这两个键。schema 层做强类型互转, 旧章节缺键时
# 退化为 frame_step=None / source="manual"。
ChapterSource = Literal["manual", "sampled"]
_META_FRAME_STEP = "frame_step"
_META_SOURCE = "source"


class VideoChapterBase(BaseModel):
    start_frame: int = Field(ge=0)
    end_frame: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=200)
    color: str | None = Field(default=None, max_length=40)
    metadata: dict[str, Any] = Field(default_factory=dict)
    # 该章节内建议的逐帧步长 (源帧空间); 给出时必须 >= 1。
    frame_step: int | None = Field(default=None, ge=1)
    # 章节来源: 手动建 (manual) 还是由采样网格派生 (sampled)。
    source: ChapterSource | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @model_validator(mode="after")
    def _check_frame_order(self) -> "VideoChapterBase":
        if self.end_frame < self.start_frame:
            raise ValueError("end_frame must be >= start_frame")
        return self


class VideoChapterCreate(VideoChapterBase):
    pass


class VideoChapterUpdate(BaseModel):
    start_frame: int | None = Field(default=None, ge=0)
    end_frame: int | None = Field(default=None, ge=0)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    color: str | None = Field(default=None, max_length=40)
    metadata: dict[str, Any] | None = None
    frame_step: int | None = Field(default=None, ge=1)
    source: ChapterSource | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value


class VideoChapterOut(BaseModel):
    id: UUID
    dataset_item_id: UUID
    start_frame: int
    end_frame: int
    title: str
    color: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    frame_step: int | None = None
    source: ChapterSource = "manual"
    created_by: UUID | None = None
    created_at: datetime
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def _derive_from_metadata(self) -> "VideoChapterOut":
        # frame_step / source 未显式给出时, 从 metadata 内的约定键派生 (向后兼容)。
        meta = self.metadata or {}
        if self.frame_step is None:
            raw_step = meta.get(_META_FRAME_STEP)
            if isinstance(raw_step, int) and raw_step >= 1:
                self.frame_step = raw_step
        raw_source = meta.get(_META_SOURCE)
        if raw_source in ("manual", "sampled"):
            self.source = raw_source
        return self

    class Config:
        from_attributes = True


def merge_chapter_metadata(
    base: dict[str, Any] | None,
    *,
    frame_step: int | None,
    source: ChapterSource | None,
) -> dict[str, Any]:
    """把强类型的 frame_step / source 写回 chapter_metadata 约定键。

    base 为已有 metadata (Create 时通常为空, Update 时为请求里的 metadata 或现有值)。
    显式给出的字段覆盖对应键; 未给出时保持 base 原值不动。
    """
    merged = dict(base or {})
    if frame_step is not None:
        merged[_META_FRAME_STEP] = frame_step
    if source is not None:
        merged[_META_SOURCE] = source
    return merged


def snap_chapter_to_grid(
    start_frame: int, end_frame: int, step: int
) -> tuple[int, int]:
    """把章节 [start_frame, end_frame] 对齐到采样网格 (锚定 0 的 step 倍数)。

    - start 向下取整到 step 的倍数: floor(start / step) * step
    - end 向下贴到 <= end 的最近网格点: floor(end / step) * step
      (即「向上对齐到网格点且不超过 end」: 不允许越过原 end, 故取其下的网格点)
    step <= 1 时网格即全部帧, 原样返回。结果保证 start <= end。
    """
    step = max(1, int(step))
    if step == 1:
        return start_frame, end_frame
    aligned_start = (start_frame // step) * step
    aligned_end = (end_frame // step) * step
    if aligned_end < aligned_start:
        aligned_end = aligned_start
    return aligned_start, aligned_end


class VideoChapterList(BaseModel):
    chapters: list[VideoChapterOut] = Field(default_factory=list)
