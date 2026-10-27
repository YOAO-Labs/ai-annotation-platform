"""Project-scope guard for trained-model references in saved pipelines.

Main kept this check in the retired ``app.services.pipeline_template`` facade;
the facade itself is frozen legacy on this branch, so the surviving rule lives
here and is applied by the saved-pipeline routes before the owner command.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from app.services.trained_models import is_controlled, load_version


async def assert_model_reference_scope(db, stages, scope, project_id) -> None:
    """A controlled project model may only be referenced by its own private pipeline."""
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        model_id = stage.get("model_id")
        if not is_controlled(model_id):
            continue
        if scope != "private" or project_id is None:
            raise HTTPException(
                status_code=422, detail="项目训练模型只能保存在所属项目的私有管线中"
            )
        if model_id.startswith("trained:"):
            try:
                version_id = uuid.UUID(model_id.removeprefix("trained:"))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="模型版本标识无效") from exc
            await load_version(db, project_id, version_id)
