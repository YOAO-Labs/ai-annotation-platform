from __future__ import annotations

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.video_chapter import (
    VideoChapterCreate,
    VideoChapterOut,
    VideoChapterUpdate,
    merge_chapter_metadata,
)


_READ = text("""
    SELECT * FROM yoao_assets_v1.read_legacy_video_chapters(:item,:task,:actor)
    ORDER BY start_frame, end_frame
""")
_LOCK_SOURCE = text("""
    SELECT yoao_assets_v1.lock_legacy_video_chapter_source(:item,:task,:actor)
""")
_LOCK = text("""
    SELECT * FROM yoao_assets_v1.lock_legacy_video_chapter(:item,:task,:actor,:chapter)
""")
_CREATE = text("""
    SELECT * FROM yoao_assets_v1.create_legacy_video_chapter(
        :item,:task,:actor,:chapter,:start_frame,:end_frame,:title,:color,
        CAST(:metadata AS jsonb)
    )
""")
_UPDATE = text("""
    SELECT * FROM yoao_assets_v1.update_legacy_video_chapter(
        :item,:task,:actor,:chapter,:start_frame,:end_frame,:title,:color,
        CAST(:metadata AS jsonb)
    )
""")
_DELETE = text("""
    SELECT start_frame,end_frame,title
    FROM yoao_assets_v1.delete_legacy_video_chapter(:item,:task,:actor,:chapter)
""")


async def list_chapters(
    db: AsyncSession, item_id: uuid.UUID, task_id: uuid.UUID, actor_id: uuid.UUID
) -> list[VideoChapterOut]:
    if db.autoflush:
        await db.flush()
    rows = await db.execute(
        _READ, {"item": item_id, "task": task_id, "actor": actor_id}
    )
    return [VideoChapterOut.model_validate(row) for row in rows.mappings()]


async def lock_chapter_source(
    db: AsyncSession, item_id: uuid.UUID, task_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    return (
        await db.scalar(
            _LOCK_SOURCE, {"item": item_id, "task": task_id, "actor": actor_id}
        )
        is True
    )


async def lock_chapter(
    db: AsyncSession,
    item_id: uuid.UUID,
    task_id: uuid.UUID,
    actor_id: uuid.UUID,
    chapter_id: uuid.UUID,
) -> VideoChapterOut:
    rows = await db.execute(
        _LOCK,
        {"item": item_id, "task": task_id, "actor": actor_id, "chapter": chapter_id},
    )
    row = rows.mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "章节不存在")
    return VideoChapterOut.model_validate(row)


async def _write_chapter(db: AsyncSession, statement, args: dict) -> VideoChapterOut:
    try:
        await db.flush()
        rows = await db.execute(statement, args)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "章节起止帧与现有章节重复") from None
    row = rows.mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "章节不存在")
    return VideoChapterOut.model_validate(row)


async def create_chapter(
    db: AsyncSession,
    *,
    dataset_item_id: uuid.UUID,
    task_id: uuid.UUID,
    actor_id: uuid.UUID,
    payload: VideoChapterCreate,
) -> VideoChapterOut:
    metadata = merge_chapter_metadata(
        payload.metadata, frame_step=payload.frame_step, source=payload.source
    )
    return await _write_chapter(
        db,
        _CREATE,
        {
            "item": dataset_item_id,
            "task": task_id,
            "actor": actor_id,
            "chapter": uuid.uuid4(),
            "start_frame": payload.start_frame,
            "end_frame": payload.end_frame,
            "title": payload.title,
            "color": payload.color,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        },
    )


async def update_chapter(
    db: AsyncSession,
    *,
    chapter: VideoChapterOut,
    task_id: uuid.UUID,
    actor_id: uuid.UUID,
    payload: VideoChapterUpdate,
) -> tuple[VideoChapterOut, dict[str, object]]:
    changes: dict[str, object] = {}
    start, end = chapter.start_frame, chapter.end_frame
    title, color = chapter.title, chapter.color
    metadata = chapter.metadata
    if payload.start_frame is not None:
        start = changes["start_frame"] = payload.start_frame
    if payload.end_frame is not None:
        end = changes["end_frame"] = payload.end_frame
    if end < start:
        raise HTTPException(422, "end_frame 必须 >= start_frame")
    if payload.title is not None:
        title = changes["title"] = payload.title
    if payload.color is not None:
        color = changes["color"] = payload.color
    if (
        payload.metadata is not None
        or payload.frame_step is not None
        or payload.source is not None
    ):
        metadata = merge_chapter_metadata(
            payload.metadata if payload.metadata is not None else metadata,
            frame_step=payload.frame_step,
            source=payload.source,
        )
        changes["metadata"] = True
    if not changes:
        return chapter, changes
    updated = await _write_chapter(
        db,
        _UPDATE,
        {
            "item": chapter.dataset_item_id,
            "task": task_id,
            "actor": actor_id,
            "chapter": chapter.id,
            "start_frame": start,
            "end_frame": end,
            "title": title,
            "color": color,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        },
    )
    return updated, changes


async def delete_chapter(
    db: AsyncSession,
    *,
    dataset_item_id: uuid.UUID,
    task_id: uuid.UUID,
    actor_id: uuid.UUID,
    chapter_id: uuid.UUID,
) -> dict[str, object]:
    rows = await db.execute(
        _DELETE,
        {
            "item": dataset_item_id,
            "task": task_id,
            "actor": actor_id,
            "chapter": chapter_id,
        },
    )
    row = rows.mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "章节不存在")
    return dict(row)
