"""Retained source references and the protection they provide against deletion."""

from __future__ import annotations

from fastapi import HTTPException
from app.services import active_learning_data


async def retain_references(db, round, references):
    if references:
        await active_learning_data.retain_history(db, round.id, references)


async def guard_source(
    db,
    *,
    project_id=None,
    dataset_id=None,
    item_ids=None,
    task_ids=None,
    version_id=None,
    evaluation_id=None,
):
    """Call before any destructive object or child-row mutation."""
    if all(
        value is None
        for value in (
            project_id,
            dataset_id,
            item_ids,
            task_ids,
            version_id,
            evaluation_id,
        )
    ):
        raise ValueError("A specific active learning source is required")
    if await active_learning_data.history_reference(
        db, project_id, dataset_id, item_ids, task_ids, version_id, evaluation_id
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "active_learning_reference",
                "message": "主动学习历史仍引用此来源；未删除数据或存储对象",
            },
        )
