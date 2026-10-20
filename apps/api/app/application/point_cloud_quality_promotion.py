"""Compose point-cloud evaluation promotion in the caller transaction."""

from __future__ import annotations

import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models.project import Project
from app.db.models.point_cloud_quality import PointCloudQualityEvaluation
from app.services.project_workspace import set_point_cloud_quality_config
from app.services.point_cloud_quality.evaluation import (
    prepare_evaluation_promotion,
    record_evaluation_promotion,
)
from app.services.point_cloud_quality.service import PointCloudQualityError


async def promote_point_cloud_quality_evaluation(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    evaluation_id: uuid.UUID,
    actor_id: uuid.UUID,
) -> tuple[PointCloudQualityEvaluation, Project]:
    project = (
        await db.execute(
            select(Project).where(Project.id == project_id).with_for_update()
        )
    ).scalar_one_or_none()
    evaluation = await db.get(PointCloudQualityEvaluation, evaluation_id)
    if project is None or evaluation is None or evaluation.project_id != project_id:
        raise PointCloudQualityError(404, "point_cloud_quality_evaluation_not_found")
    candidate = prepare_evaluation_promotion(
        evaluation,
        current_config=project.point_cloud_quality_config,
    )
    set_point_cloud_quality_config(
        project=project, config=candidate.model_dump(mode="json")
    )
    record_evaluation_promotion(
        evaluation,
        actor_id=actor_id,
        config_revision=candidate.config_revision,
    )
    await db.flush()
    return evaluation, project
