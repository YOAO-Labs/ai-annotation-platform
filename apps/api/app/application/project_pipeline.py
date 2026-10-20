"""Project-level composition for applying saved inference pipelines."""

from __future__ import annotations

import uuid

from aap_inference import validate_saved_pipeline
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project_pipeline import ProjectPipeline
from app.services.project_access import lock_current_project_access
from app.services.pipeline_template import apply_pipeline, unenabled_backend_ids


async def apply_saved_pipeline(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    project_id: uuid.UUID,
    source: ProjectPipeline,
    set_default: bool,
) -> ProjectPipeline:
    canonical_stages = validate_saved_pipeline(source.stages)
    missing = await unenabled_backend_ids(db, project_id, source.stages)
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "编排引用的 backend 未在当前项目启用",
                "unenabled_backends": missing,
            },
        )

    _actor, project, access = await lock_current_project_access(
        db, actor_id=actor_id, project_id=project_id
    )
    if not access.is_manager:
        raise HTTPException(status_code=403, detail="仅项目负责人或超级管理员可执行")
    return await apply_pipeline(
        db,
        source=source,
        project_id=project.id,
        creator_id=actor_id,
        canonical_stages=canonical_stages,
        set_default=set_default,
    )
