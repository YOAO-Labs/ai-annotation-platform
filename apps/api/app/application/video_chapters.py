from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.task_workflow_locks import (
    lock_current_task_target,
    lock_row_nowait,
)
from app.db.models.dataset import DatasetItem
from app.db.models.video_chapter import VideoChapter
from app.schemas.video_chapter import VideoChapterCreate, VideoChapterUpdate
from app.services import task_access
from aap_workspace_contract.access import ProjectAccess
from app.services.video_chapter_service import (
    create_chapter,
    delete_chapter,
    update_chapter,
)


class VideoChapterWorkflow:
    """Fence current video authority and target before chapter-row commands."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        *,
        dataset_item_id: uuid.UUID,
        task_id: uuid.UUID,
        actor_id: uuid.UUID,
        expected_access: ProjectAccess,
        payload: VideoChapterCreate,
    ) -> VideoChapter:
        await self._lock_current_video_target(
            dataset_item_id=dataset_item_id,
            task_id=task_id,
            actor_id=actor_id,
            expected_access=expected_access,
        )
        return await create_chapter(
            self.db,
            dataset_item_id=dataset_item_id,
            actor_id=actor_id,
            payload=payload,
        )

    async def update(
        self,
        *,
        dataset_item_id: uuid.UUID,
        task_id: uuid.UUID,
        actor_id: uuid.UUID,
        expected_access: ProjectAccess,
        chapter_id: uuid.UUID,
        payload: VideoChapterUpdate,
    ) -> tuple[VideoChapter, dict[str, object]]:
        await self._lock_current_video_target(
            dataset_item_id=dataset_item_id,
            task_id=task_id,
            actor_id=actor_id,
            expected_access=expected_access,
        )
        chapter = await self._lock_chapter(dataset_item_id, chapter_id)
        return await update_chapter(self.db, chapter=chapter, payload=payload)

    async def delete(
        self,
        *,
        dataset_item_id: uuid.UUID,
        task_id: uuid.UUID,
        actor_id: uuid.UUID,
        expected_access: ProjectAccess,
        chapter_id: uuid.UUID,
    ) -> dict[str, object]:
        await self._lock_current_video_target(
            dataset_item_id=dataset_item_id,
            task_id=task_id,
            actor_id=actor_id,
            expected_access=expected_access,
        )
        chapter = await self._lock_chapter(dataset_item_id, chapter_id)
        return await delete_chapter(self.db, chapter=chapter)

    async def _lock_current_video_target(
        self,
        *,
        dataset_item_id: uuid.UUID,
        task_id: uuid.UUID,
        actor_id: uuid.UUID,
        expected_access: ProjectAccess,
    ) -> None:
        task, actor, _project, current_access = await lock_current_task_target(
            self.db,
            task_id=task_id,
            actor_id=actor_id,
            expected_access=expected_access,
        )
        if task.dataset_item_id != dataset_item_id or task.file_type != "video":
            raise HTTPException(status_code=404, detail="Video not found")

        await task_access.assert_task_visible(
            self.db, task, actor.id, access=current_access
        )
        if not current_access.is_manager:
            raise HTTPException(
                status_code=403,
                detail="仅项目负责人或超级管理员可管理章节",
            )

        item = await lock_row_nowait(
            self.db,
            select(DatasetItem)
            .where(DatasetItem.id == dataset_item_id)
            .with_for_update(read=True, nowait=True)
            .execution_options(populate_existing=True),
            reason="video_source_busy",
        )
        if item is None or item.file_type != "video" or task.dataset_item_id != item.id:
            raise HTTPException(status_code=404, detail="Video not found")

    async def _lock_chapter(
        self, dataset_item_id: uuid.UUID, chapter_id: uuid.UUID
    ) -> VideoChapter:
        chapter = await lock_row_nowait(
            self.db,
            select(VideoChapter)
            .where(
                VideoChapter.id == chapter_id,
                VideoChapter.dataset_item_id == dataset_item_id,
            )
            .with_for_update(nowait=True)
            .execution_options(populate_existing=True),
            reason="video_chapter_busy",
        )
        if chapter is None:
            raise HTTPException(status_code=404, detail="章节不存在")
        return chapter
