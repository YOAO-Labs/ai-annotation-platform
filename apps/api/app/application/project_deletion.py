"""Project deletion with current authority and caller-owned atomic commit."""

from typing import NoReturn
from uuid import UUID, uuid4

from aap_workspace.project_deletion_contract import (
    ProjectDeletionFailure,
    ProjectDeletionReceipt,
)
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aap_audit_contract import ProjectDeletionAuditContext
from app.db.models.project import Project
from app.services.project_deletion_data import ProjectDeletionData

_RETAINED = {
    "retained_training_studies": "项目包含保留的参数搜索计划与预算证据，不能删除",
    "retained_trained_models": "项目包含候选模型或决策历史，来源数据须保留",
    "retained_model_decisions": "项目包含候选模型或决策历史，来源数据须保留",
    "retained_sample_evaluations": "项目包含冻结样本评估，请先结束并删除评估",
    "retained_learning_policies": "项目包含自动训练策略，请先关闭并移除策略及其依赖",
    "retained_training_dataset_versions": "项目包含训练数据版本，请先删除无引用的数据版本",
    "retained_training_runs": "项目包含保留的训练作业，不能删除",
    "retained_training_previews": "项目包含保留的训练预览，不能删除",
    "retained_learning_triggers": "项目包含保留的自动训练触发记录，不能删除",
}


def _restore_failure(exc: ProjectDeletionFailure) -> NoReturn:
    if exc.code in _RETAINED:
        raise HTTPException(status_code=409, detail=_RETAINED[exc.code]) from exc
    if exc.code == "active_learning_reference":
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": "主动学习历史仍引用此来源；未删除数据或存储对象",
            },
        ) from exc
    if exc.code == "video_execution_not_retired":
        raise HTTPException(
            status_code=409,
            detail={
                "code": exc.code,
                "message": "存在尚未退役的视频执行，请先取消并完成退役后重试",
            },
        ) from exc
    if exc.code == "account_not_active":
        raise HTTPException(
            status_code=401,
            detail="账号已停用或不存在",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    if exc.code == "project_missing":
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    if exc.code in {
        "account_busy",
        "project_busy",
        "membership_busy",
        "operation_conflict",
    }:
        raise HTTPException(status_code=409, detail={"reason": exc.code}) from exc
    denied = {
        "invalid_account_role": "账号角色无效，访问被拒绝",
        "invalid_project_role": "项目职责无效，访问被拒绝",
        "membership_incompatible": "账号平台角色与项目职责不兼容",
        "project_manager_required": "仅项目负责人或超级管理员可执行",
    }
    if exc.code in denied:
        raise HTTPException(status_code=403, detail=denied[exc.code]) from exc
    raise HTTPException(
        status_code=500, detail="Project deletion contract failed"
    ) from exc


async def delete_project(
    db: AsyncSession,
    project_id: UUID,
    *,
    actor_id: UUID,
    operation_id: UUID | None = None,
    audit_context: ProjectDeletionAuditContext | None = None,
) -> ProjectDeletionReceipt:
    try:
        receipt = await ProjectDeletionData(db).delete(
            operation_id=operation_id if operation_id is not None else uuid4(),
            actor_id=actor_id,
            project_id=project_id,
            audit_context=audit_context,
        )
    except ProjectDeletionFailure as exc:
        _restore_failure(exc)
    cached = db.sync_session.identity_map.get((Project, (project_id,), None))
    if cached is not None:
        db.expunge(cached)
    return receipt
