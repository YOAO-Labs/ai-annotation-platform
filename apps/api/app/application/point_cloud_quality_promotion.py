"""Compose point-cloud evaluation promotion in the caller transaction."""

from __future__ import annotations

import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.project import Project
from app.db.models.point_cloud_quality import PointCloudQualityEvaluation
from app.services.project_workspace import (
    lock_project_row,
    set_point_cloud_quality_config,
)
from app.services.point_cloud_quality.evaluation import (
    prepare_evaluation_promotion,
)
from app.services.point_cloud_quality.service import PointCloudQualityError
from app.services.labeling_spatial_quality import quality_row, spatial_quality_data
from datetime import datetime, timezone


async def promote_point_cloud_quality_evaluation(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    evaluation_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> tuple[PointCloudQualityEvaluation, Project]:
    project = await lock_project_row(
        db,
        project_id,
        nowait=False,
        populate_existing=False,
    )
    result = await spatial_quality_data(
        db, "get_point_evaluation", evaluation_id=evaluation_id
    )
    evaluation = (
        quality_row(PointCloudQualityEvaluation, result["row"])
        if result["row"]
        else None
    )
    if project is None or evaluation is None or evaluation.project_id != project_id:
        raise PointCloudQualityError(404, "point_cloud_quality_evaluation_not_found")
    candidate = prepare_evaluation_promotion(
        evaluation,
        current_config=project.point_cloud_quality_config,
    )
    await set_point_cloud_quality_config(
        db, project=project, config=candidate.model_dump(mode="json")
    )
    result = await spatial_quality_data(
        db,
        "promote_point_evaluation",
        evaluation_id=evaluation_id,
        actor_id=actor_id,
        promoted_config_revision=candidate.config_revision,
        promoted_at=datetime.now(timezone.utc).isoformat(),
    )
    await db.flush()
    evaluation = quality_row(PointCloudQualityEvaluation, result["row"])
    return evaluation, project
