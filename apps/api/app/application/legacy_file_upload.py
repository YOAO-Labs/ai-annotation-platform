from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from fastapi import HTTPException

from sqlalchemy.ext.asyncio import AsyncSession
from aap_assets_images.contract import (
    AcceptedImageWork,
    ImageBackfillSelection,
    ImageBackfillSpec,
    ImageFailure,
    ImageWorkReference,
    ScheduleImageMediaRequest,
)
from aap_assets_source_runtime.image_admission import (
    ImageAdmission,
    object_identity_from_head,
)

from app.application.task_workflow_errors import TaskWorkflowError
from app.application.task_workflow_locks import lock_current_task_target
from app.services.identity_account_lock_data import IdentityAccountLockData

from app.services.labeling_tasks import (
    LabelingTaskService,
    LabelingUploadTaskNotFound,
    LabelingUploadTaskNotUploading,
    LabelingUploadTargetChanged,
)
from app.services.project_access import (
    bind_project_write_policy_resolver,
    resolve_project_access_by_id,
    restore_project_write_policy_failure,
)
from aap_workspace_contract.access import (
    ProjectAccess,
    ProjectWritePolicyFailure,
    ProjectWritePolicyRequest,
)
from app.services.storage import StorageService
from app.services.assets_images import image_gateway, image_policy


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
            actor = await IdentityAccountLockData(self.db).read_actor_status(actor_id)
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
    ) -> UploadCompleteResult:
        try:
            target = await self.labeling_tasks.read_upload_target(task_id)
        except LabelingUploadTaskNotFound as exc:
            raise UploadTaskNotFoundError from exc
        access = await self._manager_access(actor_id, target.project_id, lock=False)
        if target.status != "uploading":
            raise UploadTaskNotUploadingError
        metadata = await asyncio.to_thread(self.storage.verify_upload, target.file_path)
        if not metadata:
            raise UploadObjectMissingError
        object_identity = None
        if target.file_type == "image":
            try:
                object_identity = object_identity_from_head(metadata)
            except ImageFailure as exc:
                raise TaskWorkflowError(409, {"reason": "source_changed"}) from exc

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
            await self.labeling_tasks.complete_uploading_task(task, target)
        except LabelingUploadTaskNotFound as exc:
            raise UploadTaskNotFoundError from exc
        except LabelingUploadTaskNotUploading as exc:
            raise UploadTaskNotUploadingError from exc
        except LabelingUploadTargetChanged as exc:
            raise TaskWorkflowError(409, {"reason": "task_target_changed"}) from exc
        if task.file_type == "image":
            if object_identity is None:
                raise TaskWorkflowError(502, {"reason": "invalid_image_identity"})
            try:
                await ImageAdmission(
                    image_gateway(self.db, caller_transaction=True), image_policy()
                ).admit_task(
                    task_id=task.id,
                    source_generation=task.media_source_generation,
                    file_path=task.file_path,
                    object_identity=object_identity,
                )
            except ImageFailure as exc:
                if exc.code in {
                    "source_changed",
                    "source_missing",
                    "source_not_ready",
                    "asset_missing",
                    "not_image",
                    "owner_changed",
                    "dependency_changed",
                    "operation_conflict",
                }:
                    raise TaskWorkflowError(
                        409, {"reason": "task_target_changed"}
                    ) from exc
                if exc.code in {
                    "task_busy",
                    "source_busy",
                    "account_busy",
                    "operation_busy",
                }:
                    raise TaskWorkflowError(409, {"reason": exc.code}) from exc
                if exc.code == "storage_unavailable":
                    raise TaskWorkflowError(
                        503, {"reason": "storage_unavailable"}
                    ) from exc
                raise TaskWorkflowError(
                    502, {"reason": "image_admission_failed"}
                ) from exc
        await self.db.commit()
        return UploadCompleteResult(
            scope=LegacyFileUploadScope(actor_id, task.project_id, task.id)
        )

    async def accept_thumbnail_backfill(
        self,
        *,
        actor_id: uuid.UUID,
        project_id: uuid.UUID,
        operation_id: uuid.UUID,
    ) -> AcceptedImageWork:
        await self._manager_access(actor_id, project_id, lock=True)
        request = ScheduleImageMediaRequest(
            operation_id=operation_id,
            owner_kind=None,
            owner_id=None,
            spec=ImageBackfillSpec(
                selection=ImageBackfillSelection(
                    owner_kind="task",
                    project_id=project_id,
                    purpose="thumbnail",
                    only_missing=True,
                ),
                policy=image_policy(),
            ),
        )
        try:
            async with image_gateway() as owner:
                accepted = await owner.call("schedule_image_media", request)
        except ImageFailure as exc:
            status = {
                "invalid_request": 422,
                "permission_denied": 403,
                "account_not_active": 401,
                "invalid_account_role": 403,
                "source_missing": 404,
                "asset_missing": 404,
                "owner_changed": 409,
                "operation_conflict": 409,
                "operation_busy": 409,
                "source_busy": 409,
                "source_changed": 409,
                "resource_budget_exceeded": 413,
                "invalid_response": 502,
                "storage_unavailable": 503,
            }.get(exc.code, 503)
            raise TaskWorkflowError(status, {"reason": exc.code}) from exc
        if (
            not isinstance(accepted, AcceptedImageWork)
            or accepted.operation_id != operation_id
            or accepted.work.kind != "image_backfill"
            or accepted.work.owner is not None
            or not isinstance(accepted.work, ImageWorkReference)
            or not isinstance(accepted.spec, ImageBackfillSpec)
            or accepted.spec.selection.owner_kind != "task"
            or accepted.spec.selection.project_id != project_id
            or accepted.spec.selection.purpose != "thumbnail"
            or accepted.spec.selection.accepted_before is None
        ):
            raise TaskWorkflowError(502, {"reason": "invalid_image_backfill_receipt"})
        return accepted
