from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.video_chapter import VideoChapter
from app.schemas.video_chapter import (
    VideoChapterCreate,
    VideoChapterUpdate,
    merge_chapter_metadata,
)


async def create_chapter(
    db: AsyncSession,
    *,
    dataset_item_id: uuid.UUID,
    actor_id: uuid.UUID,
    payload: VideoChapterCreate,
) -> VideoChapter:
    chapter = VideoChapter(
        dataset_item_id=dataset_item_id,
        start_frame=payload.start_frame,
        end_frame=payload.end_frame,
        title=payload.title,
        color=payload.color,
        # frame_step / source 落进 chapter_metadata 约定键 (不新增数据库列)。
        chapter_metadata=merge_chapter_metadata(
            payload.metadata,
            frame_step=payload.frame_step,
            source=payload.source,
        ),
        created_by=actor_id,
    )
    db.add(chapter)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="章节起止帧与现有章节重复",
        )
    return chapter


async def update_chapter(
    db: AsyncSession,
    *,
    chapter: VideoChapter,
    payload: VideoChapterUpdate,
) -> tuple[VideoChapter, dict[str, object]]:
    changes: dict[str, object] = {}
    if payload.start_frame is not None:
        chapter.start_frame = payload.start_frame
        changes["start_frame"] = payload.start_frame
    if payload.end_frame is not None:
        chapter.end_frame = payload.end_frame
        changes["end_frame"] = payload.end_frame
    if chapter.end_frame < chapter.start_frame:
        raise HTTPException(status_code=422, detail="end_frame 必须 >= start_frame")
    if payload.title is not None:
        chapter.title = payload.title
        changes["title"] = payload.title
    if payload.color is not None:
        chapter.color = payload.color
        changes["color"] = payload.color
    # metadata / frame_step / source 都写进 chapter_metadata: 给出 metadata 时以它为基底,
    # 否则在现有值上叠加; frame_step / source 显式给出时覆盖对应键。
    if (
        payload.metadata is not None
        or payload.frame_step is not None
        or payload.source is not None
    ):
        base = (
            payload.metadata
            if payload.metadata is not None
            else chapter.chapter_metadata
        )
        chapter.chapter_metadata = merge_chapter_metadata(
            base,
            frame_step=payload.frame_step,
            source=payload.source,
        )
        changes["metadata"] = True

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="章节起止帧与现有章节重复",
        )
    return chapter, changes


async def delete_chapter(
    db: AsyncSession,
    *,
    chapter: VideoChapter,
) -> dict[str, object]:
    snapshot = {
        "start_frame": chapter.start_frame,
        "end_frame": chapter.end_frame,
        "title": chapter.title,
    }
    await db.delete(chapter)
    return snapshot
