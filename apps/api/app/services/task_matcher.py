"""跨实例 task 匹配 (v0.10.15).

ROADMAP §6 决策底线: 跨实例匹配走 display_id + file_path + schema_version 三元组,
**不**用 task.id 数字 UUID. task 表当前 schema:
- display_id: String(30), UNIQUE (全局, 不仅项目级)
- (project_id, file_path): 无显式 unique 约束, 但实际由 dataset_item 扫描保证唯一

匹配规则 (oneof, display_id 优先):
1. 给 display_id: 全局查; 命中后校验 project_id 一致, 否则不匹配 (跨项目入侵).
2. 给 file_path: 项目内查; 先按相对目录路径精确匹配 (消除同名跨目录误匹配),
   匹配不到再退回历史的整串 file_path 等值匹配 (兼容).
3. 都没给: 不匹配.

相对目录匹配 (v0.10.27 导入对称):
- 库内 `task.file_path` 是完整 MinIO key, 带 dataset 名前缀 (如
  `{dataset}/animals/cat/001.jpg`); 外部文件 (COCO `image.file_name` 等) 通常是
  去前缀的相对路径 (如 `animals/cat/001.jpg`)。
- 故对带相对目录的入参, 按「`file_path` 整串等值 或 以 `/{rel}` 结尾」匹配 ——
  天然吸收 dataset 名前缀, 无需在匹配层知道前缀, 与导出镜像 (file_path 去前缀的
  相对路径) 对称。
- 仅当入参含子目录 (`/`) 时才走这层; 纯叶子名仍走历史等值匹配, 不改既有行为。

匹配失败的 entry 由调用方累计到 ImportResult.errors[], 不让整批失败 (lenient).
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.task import Task
from aap_labeling.annotation_transfer_contract import (
    ANNOTATION_TRANSFER_PROTOCOL,
    ResolveTask,
    ResolveTaskByStem,
    ResolveVideoSequence,
)
from app.services.labeling_annotation_transfer import (
    AnnotationTransferRejected,
    annotation_transfer_command,
)
from app.services.labeling_core_read_relations import get_core_row, task_row


async def _transfer(db: AsyncSession, command):
    try:
        return await annotation_transfer_command(db, command)
    except AnnotationTransferRejected as exc:
        detail = {"reason": exc.reason}
        if exc.detail is not None:
            detail["detail"] = exc.detail
        raise HTTPException(status_code=exc.status_code, detail=detail) from exc


def normalize_file_stem_path(file_path: str) -> str:
    """Normalize an external label path into a slash-separated path without ext.

    YOLO labels commonly arrive as either ``labels/foo/bar.txt`` or exported
    mirror paths like ``<project>/<dataset>/labels/foo/bar.txt``.  Matching uses
    the original image path stem, so strip everything through the last labels/
    component before removing the extension.
    """

    rel = file_path.replace("\\", "/").lstrip("/")
    parts = [p for p in rel.split("/") if p and p not in (".", "..")]
    if "labels" in parts:
        idx = len(parts) - 1 - list(reversed(parts)).index("labels")
        if idx < len(parts) - 1:
            parts = parts[idx + 1 :]
    rel = "/".join(parts)
    if rel.lower().endswith(".txt"):
        rel = rel[:-4]
    elif "." in rel.rsplit("/", 1)[-1]:
        head, _, leaf = rel.rpartition("/")
        stem = leaf.rsplit(".", 1)[0]
        rel = f"{head}/{stem}" if head else stem
    return rel.strip("/")


async def resolve_task_by_file_stem(
    db: AsyncSession,
    project_id: uuid.UUID,
    file_path: str,
) -> tuple[Task | None, str | None]:
    """Match a task by file stem, returning an ambiguity/miss reason.

    This is intentionally separate from ``resolve_task`` because stem matching
    is lossy: ``img.txt`` may correspond to ``img.jpg`` or ``img.png`` and may
    also be duplicated under different subdirectories.  Ambiguous hits are not
    guessed.
    """

    rel = normalize_file_stem_path(file_path)
    if not rel:
        return None, "empty file stem"

    data = await _transfer(
        db,
        ResolveTaskByStem(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="resolve_task_by_stem",
            project_id=project_id,
            normalized_path=rel,
        ),
    )
    if not data.tasks:
        return None, data.reason or "task not found in project"
    if len(data.tasks) > 1:
        return None, data.reason or f"ambiguous task file stem: {rel}"
    task = await _task_from_match(db, data.tasks[0])
    return (task, None) if task is not None else (None, "task not found in project")


async def resolve_task(
    db: AsyncSession,
    project_id: uuid.UUID,
    match: dict[str, Any] | None,
) -> Task | None:
    """按 display_id / file_path 解析 task; 跨项目命中 display_id 视为不匹配."""

    if not isinstance(match, dict):
        return None

    display_id = match.get("display_id")
    file_path = match.get("file_path")
    data = await _transfer(
        db,
        ResolveTask(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="resolve_task",
            project_id=project_id,
            display_id=display_id if isinstance(display_id, str) else None,
            file_path=file_path if isinstance(file_path, str) else None,
        ),
    )
    return await _task_from_match(db, data.task) if data.task is not None else None


async def resolve_task_by_video_sequence(
    db: AsyncSession, project_id: uuid.UUID, sequence: str
) -> Task | None:
    data = await _transfer(
        db,
        ResolveVideoSequence(
            schema=ANNOTATION_TRANSFER_PROTOCOL,
            operation="resolve_video_sequence",
            project_id=project_id,
            target=sequence.strip("/"),
        ),
    )
    return await _task_from_match(db, data.task) if data.task is not None else None


async def _task_from_match(db: AsyncSession, snapshot) -> Task | None:
    """Hydrate the selected id through Labeling's public row relation."""
    task = await get_core_row(db, task_row, snapshot.id)
    if task is None or any(
        getattr(task, field) != getattr(snapshot, field)
        for field in (
            "project_id",
            "dataset_item_id",
            "display_id",
            "file_name",
            "file_path",
            "file_type",
        )
    ):
        return None
    return task
