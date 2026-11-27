"""Resolve retained training weights under their original attempt owner."""

from fastapi import HTTPException

from app.services import training_run_data, training_run_storage
from app.services.training_dataset_bridge import TrainingDatasetError


def owner_for_attempt(run, attempt, *artifacts):
    """Resolve physical ownership without changing any frozen model payload."""
    if run is None or attempt is None or attempt.run_id != run.id:
        raise HTTPException(status_code=409, detail="训练产物的来源执行不一致")
    try:
        owner = training_run_storage.resolve(run)
        for artifact in artifacts:
            training_run_storage.expect_attempt(owner, attempt.id, artifact["key"])
        return owner
    except TrainingDatasetError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from None
    except (KeyError, TypeError):
        raise HTTPException(
            status_code=409, detail="训练产物缺少固定对象定位"
        ) from None


async def owner_for_model(db, version):
    """Models retain the referenced run namespace instead of duplicating it."""
    run = await training_run_data.get_run(db, version.project_id, version.run_id)
    attempt = await training_run_data.get_attempt(db, version.attempt_id)
    if (
        run is None
        or attempt is None
        or run.id != version.run_id
        or run.project_id != version.project_id
        or attempt.id != version.attempt_id
        or attempt.run_id != run.id
        or not isinstance(version.artifact, dict)
        or not isinstance(attempt.artifacts, dict)
        or version.artifact.get("role") != "weights"
        or not any(
            artifact == version.artifact
            for artifact in (attempt.artifacts or {}).values()
        )
    ):
        raise HTTPException(status_code=409, detail="模型缺少一致的保留权重来源")
    return owner_for_attempt(run, attempt, version.artifact)
