"""Restore existing HTTP failures at initial task creation entry points."""

from typing import NoReturn

from fastapi import HTTPException

from aap_workspace.configuration_policy import canonical_media_kind
from app.services.task_creation_contract import TaskCreationFailure


def raise_task_creation_http_failure(failure: TaskCreationFailure) -> NoReturn:
    code = failure.code
    if code == "account_not_active":
        raise HTTPException(
            status_code=401,
            detail="账号已停用或不存在",
            headers={"WWW-Authenticate": "Bearer"},
        ) from failure
    if code in {"account_busy", "project_busy", "membership_busy"}:
        raise HTTPException(status_code=409, detail={"reason": code}) from failure
    if code == "project_missing":
        raise HTTPException(status_code=404, detail="项目不存在") from failure
    if code == "job_missing":
        raise HTTPException(status_code=404, detail="async_job not found") from failure
    if code == "dataset_missing":
        raise HTTPException(
            status_code=404, detail="Dataset or project not found"
        ) from failure
    denied = {
        "invalid_account_role": "账号角色无效，访问被拒绝",
        "invalid_project_role": "项目职责无效，访问被拒绝",
        "membership_incompatible": "账号平台角色与项目职责不兼容",
        "project_manager_required": "仅项目负责人或超级管理员可关联数据集",
    }
    if code in denied:
        raise HTTPException(status_code=403, detail=denied[code]) from failure
    if code == "media_kind_mismatch":
        project = canonical_media_kind(failure.facts.get("project_data_type"))
        dataset = canonical_media_kind(failure.facts.get("dataset_data_type"))
        raise HTTPException(
            status_code=422,
            detail=f"data_type 不匹配(项目 {project} / 数据集 {dataset})",
        ) from failure
    if code == "scene_kind_mismatch":
        detail = (
            "scene 模式不匹配(scene 项目只能关联 has_scenes=true 的数据集)"
            if failure.facts.get("project_scene_mode")
            else "scene 模式不匹配(普通项目只能关联 has_scenes=false 的数据集)"
        )
        raise HTTPException(status_code=422, detail=detail) from failure
    if code in {
        "association_changed",
        "source_changed",
        "job_cancelled",
        "job_not_active",
        "operation_conflict",
        "cursor_conflict",
        "legacy_job_scope",
        "job_scope_mismatch",
    }:
        raise HTTPException(status_code=409, detail={"reason": code}) from failure
    raise HTTPException(
        status_code=500, detail="Task creation contract failed"
    ) from failure
