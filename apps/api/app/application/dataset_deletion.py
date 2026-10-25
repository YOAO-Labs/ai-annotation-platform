from __future__ import annotations

import uuid
from typing import NoReturn

from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession

from aap_assets_source.contract import SourceDeletionRequest
from aap_assets_source.deletion_contract import DatasetSourceDeletionReceipt
from app.db.models.dataset import ProjectDataset
from app.services.assets_source import AssetsSourceGateway
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.services.dataset_unlink_contract import (
    DatasetUnlinkCommand,
    DatasetUnlinkFailure,
    DatasetUnlinkPreview,
    DatasetUnlinkReceipt,
)
from app.services.dataset_unlink_data import DatasetUnlinkData
from aap_audit_contract import ProjectDeletionAuditContext


async def delete_dataset(
    db: AsyncSession,
    dataset_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    cleanup_bucket: str,
    item_id: uuid.UUID | None = None,
    operation_id: uuid.UUID | None = None,
    audit_context: ProjectDeletionAuditContext | None = None,
) -> DatasetSourceDeletionReceipt:
    return await AssetsSourceGateway(db, cleanup_bucket=cleanup_bucket).delete_source(
        SourceDeletionRequest(
            operation_id=operation_id or uuid.uuid4(),
            actor_id=actor_id,
            dataset_id=dataset_id,
            item_id=item_id,
            audit_context=audit_context,
        )
    )


def _restore_unlink_failure(exc: DatasetUnlinkFailure) -> NoReturn:
    if exc.code == "account_not_active":
        raise HTTPException(
            401, "账号已停用或不存在", headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    if exc.code == "project_missing":
        raise HTTPException(404, "项目不存在") from exc
    denied = {
        "invalid_account_role": "账号角色无效，访问被拒绝",
        "invalid_project_role": "项目职责无效，访问被拒绝",
        "membership_incompatible": "账号平台角色与项目职责不兼容",
        "project_manager_required": "仅项目负责人或超级管理员可取消关联",
    }
    if exc.code in denied:
        raise HTTPException(403, denied[exc.code]) from exc
    retained = {
        "active_learning_reference": "主动学习历史仍引用此来源；未删除数据或存储对象",
        "task_feedback_retained": "任务或标注仍有反馈记录，未取消关联或删除内容",
        "annotation_reference_retained": "其他任务的标注仍引用此来源，未取消关联或删除内容",
        "source_in_use": "其他记录仍引用此来源，未取消关联或删除内容",
        "video_execution_not_retired": "存在尚未退役的视频执行，请先取消并完成退役后重试",
    }
    if exc.code in retained:
        raise HTTPException(
            409, {"code": exc.code, "message": retained[exc.code]}
        ) from exc
    if exc.code in {
        "account_busy",
        "project_busy",
        "membership_busy",
        "operation_conflict",
    }:
        raise HTTPException(409, {"reason": exc.code}) from exc
    raise HTTPException(500, "Dataset unlink contract failed") from exc


async def preview_dataset_unlink(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
) -> DatasetUnlinkPreview:
    try:
        return await DatasetUnlinkData(db).preview(
            actor_id=actor_id, dataset_id=dataset_id, project_id=project_id
        )
    except DatasetUnlinkFailure as exc:
        _restore_unlink_failure(exc)


def _invalidate_unlink_cache(db: AsyncSession, receipt: DatasetUnlinkReceipt) -> None:
    # Native owner writes bypass this assembly's ORM identity map.
    for row in list(db.sync_session.identity_map.values()):
        state = inspect(row)
        if isinstance(row, Project) and state.identity == (receipt.project_id,):
            db.expire(
                row,
                [
                    "total_tasks",
                    "completed_tasks",
                    "review_tasks",
                    "in_progress_tasks",
                    "batch_summary",
                    "updated_at",
                ],
            )
        elif isinstance(row, (Task, TaskBatch)) and state.dict.get("project_id") in (
            None,
            receipt.project_id,
        ):
            db.expunge(row)
        elif isinstance(row, ProjectDataset) and state.identity == (
            receipt.association_id,
        ):
            db.expunge(row)


async def unlink_dataset_from_project(
    db: AsyncSession,
    dataset_id: uuid.UUID,
    project_id: uuid.UUID,
    *,
    actor_id: uuid.UUID,
    cleanup_bucket: str,
    operation_id: uuid.UUID | None = None,
    audit_context: ProjectDeletionAuditContext | None = None,
) -> dict | None:
    command = DatasetUnlinkCommand(
        operation_id=operation_id or uuid.uuid4(),
        actor_id=actor_id,
        dataset_id=dataset_id,
        project_id=project_id,
        cleanup_bucket=cleanup_bucket,
        audit_context=audit_context,
    )
    try:
        receipt = await DatasetUnlinkData(db).unlink(command)
    except DatasetUnlinkFailure as exc:
        if exc.code in {"dataset_missing", "link_missing"}:
            return None
        _restore_unlink_failure(exc)
    _invalidate_unlink_cache(db, receipt)
    return receipt.public_result()
