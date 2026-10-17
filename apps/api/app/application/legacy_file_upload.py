from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import HTTPException

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.task_workflow_errors import TaskWorkflowError
from app.application.task_workflow_locks import lock_current_task_target
from app.db.models.user import User

from app.services.labeling_tasks import (
    LabelingTaskService,
    LabelingUploadTaskNotFound,
    LabelingUploadTaskNotUploading,
    LabelingUploadTargetChanged,
)
from app.services.media_job_dispatch import MediaJobDispatcher
from app.services.project_access import (
    bind_project_write_policy_resolver,
    resolve_project_access_by_id,
    restore_project_write_policy_failure,
)
from app.services.workspace_contract import (
    ProjectAccess,
    ProjectWritePolicyFailure,
    ProjectWritePolicyRequest,
)
from app.services.storage import StorageService


class UploadTaskNotFoundError(Exception):
    pass


class UploadTaskNotUploadingError(Exception):
    pass


class UploadObjectMissingError(Exception):
    pass


@dataclass(frozen=True)
class LegacyFileUploadScope:
    actor_id: uuid.UUID
    project_id: uuid.UUID
    task_id: uuid.UUID


@dataclass(frozen=True)
class UploadInitResult:
    scope: LegacyFileUploadScope
    upload_url: str
    expires_in: int


@dataclass(frozen=True)
class UploadCompleteResult:
    scope: LegacyFileUploadScope


class LegacyFileUploadWorkflow:
    def __init__(
        self,
        db: AsyncSession,
        *,
        storage: StorageService,
    ) -> None:
        self.db = db
        self.labeling_tasks = LabelingTaskService(db)
        self.storage = storage

    async def _manager_access(
        self, actor_id: uuid.UUID, project_id: uuid.UUID, *, lock: bool
    ) -> ProjectAccess:
        if lock:
            resolver = bind_project_write_policy_resolver(self.db)
            try:
                access = await resolver.resolve(
                    ProjectWritePolicyRequest(actor_id=actor_id, project_id=project_id)
                )
            except ProjectWritePolicyFailure as exc:
                restore_project_write_policy_failure(exc)
        else:
            actor = await self.db.get(User, actor_id)
            if actor is None:
                raise HTTPException(
                    status_code=401,
                    detail="账号已停用或不存在",
                    headers={"WWW-Authenticate": "Bearer"},
                )
            _, access = await resolve_project_access_by_id(
                self.db, actor_id=actor.id, project_id=project_id
            )
        if not access.is_manager:
            raise HTTPException(
                status_code=403, detail="仅项目负责人或超级管理员可执行"
            )
        return access

    async def initialize_upload(
        self,
        *,
        actor_id: uuid.UUID,
        project_id: uuid.UUID,
        file_name: str,
        content_type: str,
        file_type: str,
    ) -> UploadInitResult:
        await self._manager_access(actor_id, project_id, lock=True)
        task_id = uuid.uuid4()
        storage_key = f"{project_id}/{task_id}/{file_name}"
        await self.labeling_tasks.create_uploading_task(
            task_id=task_id,
            project_id=project_id,
            file_name=file_name,
            file_path=storage_key,
            file_type=file_type,
        )
        upload_url = self.storage.generate_upload_url(storage_key, content_type)
        return UploadInitResult(
            scope=LegacyFileUploadScope(actor_id, project_id, task_id),
            upload_url=upload_url,
            expires_in=900,
        )

    async def complete_upload(
        self,
        *,
        actor_id: uuid.UUID,
        task_id: uuid.UUID,
        dispatcher: MediaJobDispatcher,
    ) -> UploadCompleteResult:
        try:
            target = await self.labeling_tasks.read_upload_target(task_id)
        except LabelingUploadTaskNotFound as exc:
            raise UploadTaskNotFoundError from exc
        access = await self._manager_access(actor_id, target.project_id, lock=False)
        if target.status != "uploading":
            raise UploadTaskNotUploadingError
        if not self.storage.verify_upload(target.file_path):
            raise UploadObjectMissingError

        # Storage I/O holds no authority/resource row locks. Reacquire current
        # authority and the fresh task before committing the captured source.
        task, _, _, current_access = await lock_current_task_target(
            self.db, actor_id=actor_id, task_id=task_id, expected_access=access
        )
        if not current_access.is_manager:
            raise HTTPException(
                status_code=403, detail="仅项目负责人或超级管理员可执行"
            )
        try:
            await self.labeling_tasks.mark_uploading_task_pending(target)
        except LabelingUploadTaskNotFound as exc:
            raise UploadTaskNotFoundError from exc
        except LabelingUploadTaskNotUploading as exc:
            raise UploadTaskNotUploadingError from exc
        except LabelingUploadTargetChanged as exc:
            raise TaskWorkflowError(409, {"reason": "task_target_changed"}) from exc
        if task.file_type == "image":
            dispatcher.dispatch_task_thumbnail(task.id)
        return UploadCompleteResult(
            scope=LegacyFileUploadScope(actor_id, task.project_id, task.id)
        )

    async def queue_thumbnail_backfill(
        self,
        *,
        actor_id: uuid.UUID,
        project_id: uuid.UUID,
        dispatcher: MediaJobDispatcher,
    ) -> str:
        await self._manager_access(actor_id, project_id, lock=True)
        return dispatcher.dispatch_task_thumbnail_backfill(project_id)
