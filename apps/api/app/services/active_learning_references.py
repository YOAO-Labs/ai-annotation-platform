"""Retained source references and the protection they provide against deletion."""

from __future__ import annotations

import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.db.models.active_learning import (
    ActiveLearningPoolMember as Member,
    ActiveLearningReference as Reference,
    ActiveLearningRound as Round,
)
from app.db.models.dataset import DatasetItem


async def retain_references(db, round, references):
    existing = (
        await db.scalars(select(Reference).where(Reference.round_id == round.id))
    ).all()
    ids = {
        ("dataset", str(row.dataset_version_id))
        if row.dataset_version_id
        else ("evaluation", str(row.evaluation_id))
        for row in existing
    }
    for ref in references:
        if (ref["kind"], ref["id"]) in ids:
            continue
        db.add(
            Reference(
                round_id=round.id,
                dataset_version_id=uuid.UUID(ref["id"])
                if ref["kind"] == "dataset"
                else None,
                evaluation_id=uuid.UUID(ref["id"])
                if ref["kind"] == "evaluation"
                else None,
                source_digest=ref["digest"],
            )
        )
        ids.add((ref["kind"], ref["id"]))


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
    if project_id is not None:
        found = await db.scalar(
            select(Round.id).where(Round.project_id == project_id).limit(1)
        )
    elif version_id is not None or evaluation_id is not None:
        condition = (
            Reference.dataset_version_id == version_id
            if version_id is not None
            else Reference.evaluation_id == evaluation_id
        )
        found = await db.scalar(select(Reference.id).where(condition).limit(1))
    else:
        query_ref = select(Member.round_id)
        if dataset_id is not None:
            query_ref = query_ref.join(
                DatasetItem, DatasetItem.id == Member.dataset_item_id
            ).where(DatasetItem.dataset_id == dataset_id)
        elif item_ids is not None:
            query_ref = query_ref.where(Member.dataset_item_id.in_(item_ids))
        elif task_ids is not None:
            query_ref = query_ref.where(Member.task_id.in_(task_ids))
        else:
            raise ValueError("A specific active learning source is required")
        found = await db.scalar(query_ref.limit(1))
    if found:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "active_learning_reference",
                "message": "主动学习历史仍引用此来源；未删除数据或存储对象",
            },
        )
