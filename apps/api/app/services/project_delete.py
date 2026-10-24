"""Shared project deletion transaction used by the API and dev seed repair."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.db.models.training_dataset import TrainingDatasetVersion
from app.db.models.project_task_dataset import ProjectTaskDataset


async def delete_project_records(db: AsyncSession, project: Project) -> None:
    """Delete one project and its non-cascading children without committing."""
    from app.services.project_task_dataset import lock_project

    # Keep creation, metadata backfill, and project deletion in one lock order.
    await lock_project(db, project.id)
    # Serialize with snapshot creators' project SHARE lock before deleting children.
    await db.execute(
        select(Project.id).where(Project.id == project.id).with_for_update()
    )
    from app.services.active_learning_pool import guard_source

    await guard_source(db, project_id=project.id)
    from app.db.models.trained_model import TrainedModelVersion
    from app.db.models.model_decision import ModelDecision
    from app.db.models.training_study import TrainingStudy

    if await db.scalar(
        select(TrainingStudy.id).where(TrainingStudy.project_id == project.id).limit(1)
    ):
        raise HTTPException(
            status_code=409, detail="项目包含保留的参数搜索计划与预算证据，不能删除"
        )

    for model in (TrainedModelVersion, ModelDecision):
        if await db.scalar(
            select(model.id).where(model.project_id == project.id).limit(1)
        ):
            raise HTTPException(
                status_code=409, detail="项目包含候选模型或决策历史，来源数据须保留"
            )

    from app.db.models.learning_policy import LearningPolicy
    from app.db.models.sample_evaluation import SampleEvaluationRun

    if await db.scalar(
        select(SampleEvaluationRun.id)
        .where(SampleEvaluationRun.project_id == project.id)
        .limit(1)
    ):
        raise HTTPException(
            status_code=409, detail="项目包含冻结样本评估，请先结束并删除评估"
        )

    if await db.scalar(
        select(LearningPolicy.id)
        .where(LearningPolicy.project_id == project.id)
        .limit(1)
    ):
        raise HTTPException(
            status_code=409, detail="项目包含自动训练策略，请先关闭并移除策略及其依赖"
        )
    if await db.scalar(
        select(TrainingDatasetVersion.id)
        .where(TrainingDatasetVersion.project_id == project.id)
        .limit(1)
    ):
        raise HTTPException(
            status_code=409, detail="项目包含训练数据版本，请先删除无引用的数据版本"
        )
    # Empty management streams have no retained training evidence. They must not
    # prevent deletion after every frozen version was explicitly removed.
    await db.execute(
        delete(ProjectTaskDataset).where(ProjectTaskDataset.project_id == project.id)
    )
    params = {"pid": str(project.id)}
    from app.db.models.annotation_comment import AnnotationComment
    from app.services.comment_attachment_uploads import retire_for_comments

    await retire_for_comments(
        db,
        select(AnnotationComment.id).where(AnnotationComment.project_id == project.id),
    )
    await db.execute(
        text("DELETE FROM annotation_comments WHERE project_id = :pid"), params
    )
    await db.execute(
        text("DELETE FROM annotation_feedbacks WHERE project_id = :pid"), params
    )
    await db.execute(
        text(
            "DELETE FROM annotation_drafts WHERE task_id IN "
            "(SELECT id FROM tasks WHERE project_id = :pid)"
        ),
        params,
    )
    await db.execute(
        text(
            "DELETE FROM prediction_metas WHERE prediction_id IN "
            "(SELECT id FROM predictions WHERE project_id = :pid) "
            "OR failed_prediction_id IN "
            "(SELECT id FROM failed_predictions WHERE project_id = :pid)"
        ),
        params,
    )
    await db.execute(
        text(
            "UPDATE annotations SET parent_prediction_id = NULL, "
            "parent_annotation_id = NULL WHERE project_id = :pid"
        ),
        params,
    )
    await db.execute(text("DELETE FROM annotations WHERE project_id = :pid"), params)
    await db.execute(text("DELETE FROM predictions WHERE project_id = :pid"), params)
    await db.execute(
        text("DELETE FROM failed_predictions WHERE project_id = :pid"), params
    )
    await db.execute(
        text(
            "DELETE FROM task_locks WHERE task_id IN "
            "(SELECT id FROM tasks WHERE project_id = :pid)"
        ),
        params,
    )
    # Global backend registry rows are reusable and never belong to one project.
    # Project-backend association rows are removed by the project cascade.
    await db.execute(
        text("UPDATE bug_reports SET project_id = NULL WHERE project_id = :pid"),
        params,
    )
    # Historical reports can retain a task reference after their project reference
    # was cleared, so detach task references by actual project task ownership.
    await db.execute(
        text(
            "UPDATE bug_reports SET task_id = NULL WHERE task_id IN "
            "(SELECT id FROM tasks WHERE project_id = :pid)"
        ),
        params,
    )
    await db.execute(text("DELETE FROM tasks WHERE project_id = :pid"), params)
    await db.delete(project)
    await db.flush()
