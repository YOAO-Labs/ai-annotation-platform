"""Fixed scale-seed owner calls inside the original staged transactions."""

from __future__ import annotations

import json

from fastapi import HTTPException
from sqlalchemy import text

from aap_workspace.scale_seed_contract import (
    decode_scale_seed_cleanup,
    decode_scale_seed_count,
    decode_scale_seed_project_id,
    decode_scale_seed_source_guard,
)
from app.services.project_access import _locked_access_row


def _require_cleanup_allowed(reply):
    if not reply.ok:
        raise HTTPException(
            status_code=409,
            detail={
                "code": reply.error.code,
                "message": "规模种子仍有保留引用或未退役执行，请解除引用或完成退役后重试",
            },
        )


async def create_scale_seed_project(db, *, owner_id, marker, total_tasks):
    raw = await db.scalar(
        text(
            "SELECT aap_workspace_v1.create_scale_seed_project(CAST(:owner AS uuid),CAST(:marker AS text))"
        ),
        {"owner": owner_id, "marker": marker},
    )
    project_id = decode_scale_seed_project_id(raw)
    updated = await db.scalar(
        text(
            "SELECT aap_labeling_v1.write_project_counter_projection(CAST(:id AS uuid),CAST(:patch AS jsonb))"
        ),
        {
            "id": project_id,
            "patch": json.dumps({"total_tasks": total_tasks}, allow_nan=False),
        },
    )
    if updated != project_id:
        raise HTTPException(status_code=503, detail="标注项目计数服务暂不可用")
    return project_id


async def ensure_scale_seed_member(db, *, project_id, annotator_id, owner_id):
    raw = await db.scalar(
        text(
            "SELECT aap_workspace_v1.ensure_scale_seed_member(CAST(:project AS uuid),CAST(:annotator AS uuid),CAST(:owner AS uuid))"
        ),
        {"project": project_id, "annotator": annotator_id, "owner": owner_id},
    )
    return decode_scale_seed_count(raw, maximum=1)


async def guard_scale_seed_sources(db, dataset_ids, *, after_task_cleanup):
    for dataset_id in dataset_ids:
        raw = await _locked_access_row(
            db,
            text(
                "SELECT aap_assets_v1.guard_scale_seed_dataset_cleanup(CAST(:dataset AS uuid),CAST(:after_tasks AS boolean))"
            ),
            parameters={"dataset": dataset_id, "after_tasks": after_task_cleanup},
            busy_reason="source_busy",
        )
        _require_cleanup_allowed(
            decode_scale_seed_source_guard(
                raw,
                dataset_id=dataset_id,
                after_task_cleanup=after_task_cleanup,
            )
        )


async def prepare_scale_seed_cleanup(db, marker):
    dataset_ids = list(
        (
            await db.scalars(
                text("SELECT id FROM datasets WHERE name=:marker ORDER BY id"),
                {"marker": marker},
            )
        ).all()
    )
    await guard_scale_seed_sources(db, dataset_ids, after_task_cleanup=False)
    raw = await _locked_access_row(
        db,
        text("SELECT aap_workspace_v1.guard_scale_seed_cleanup(CAST(:marker AS text))"),
        parameters={"marker": marker},
        busy_reason="project_busy",
    )
    reply = decode_scale_seed_cleanup(raw, marker=marker)
    _require_cleanup_allowed(reply)
    return reply.project_ids, dataset_ids


async def delete_scale_seed_projects(db, *, marker, project_ids):
    raw = await db.scalar(
        text(
            "SELECT aap_workspace_v1.delete_scale_seed_projects(CAST(:marker AS text),CAST(:ids AS uuid[]))"
        ),
        {"marker": marker, "ids": project_ids},
    )
    deleted = decode_scale_seed_count(raw, maximum=len(project_ids))
    if deleted != len(project_ids):
        raise HTTPException(status_code=503, detail="规模种子项目清理不完整")
