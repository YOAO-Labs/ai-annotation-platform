from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.task_workflow_locks import (
    lock_current_task_target,
    lock_core_nowait,
)
from app.schemas.video_chapter import (
    VideoChapterCreate,
    VideoChapterOut,
    VideoChapterUpdate,
)
from app.services import task_access
from yoao_workspace_contract.access import ProjectAccess
from app.services.video_chapter_service import (
    create_chapter,
    delete_chapter,
    lock_chapter,
    lock_chapter_source,
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
    ) -> VideoChapterOut:
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
            task_id=task_id,
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
    ) -> tuple[VideoChapterOut, dict[str, object]]:
        await self._lock_current_video_target(
            dataset_item_id=dataset_item_id,
            task_id=task_id,
            actor_id=actor_id,
            expected_access=expected_access,
        )
        chapter = await lock_core_nowait(
            self.db,
            lambda: lock_chapter(
                self.db, dataset_item_id, task_id, actor_id, chapter_id
            ),
            reason="video_chapter_busy",
        )
        return await update_chapter(
            self.db,
            chapter=chapter,
            task_id=task_id,
            actor_id=actor_id,
            payload=payload,
        )

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
        return await lock_core_nowait(
            self.db,
            lambda: delete_chapter(
                self.db,
                dataset_item_id=dataset_item_id,
                task_id=task_id,
                actor_id=actor_id,
                chapter_id=chapter_id,
            ),
            reason="video_chapter_busy",
        )

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
        found = await lock_core_nowait(
            self.db,
            lambda: lock_chapter_source(self.db, dataset_item_id, task_id, actor_id),
            reason="video_source_busy",
        )
        if not found:
            raise HTTPException(status_code=404, detail="Video not found")
