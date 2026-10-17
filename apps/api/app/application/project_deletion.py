from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.project import Project
from app.services import (
    learning_policies,
    model_lineage,
    project_task_dataset,
    sample_evaluations,
    trained_models,
    training_datasets,
    training_studies,
)
from app.services.active_learning_references import guard_source
from app.services.bug_report import BugReportService
from app.services.feedback import FeedbackService
from app.services.labeling_tasks import LabelingTaskService
from app.services.project_workspace import delete_project as delete_project_record


async def delete_project(db: AsyncSession, project: Project) -> None:
    await project_task_dataset.lock_project(db, project.id)
    await db.execute(
        select(Project.id).where(Project.id == project.id).with_for_update()
    )
    await guard_source(db, project_id=project.id)

    await training_studies.assert_no_project_studies(db, project.id)
    await trained_models.assert_no_project_models(db, project.id)
    await model_lineage.assert_no_project_decisions(db, project.id)
    await sample_evaluations.assert_no_project_evaluations(db, project.id)
    await learning_policies.assert_no_project_policies(db, project.id)
    await training_datasets.assert_no_project_versions(db, project.id)

    await project_task_dataset.delete_project_streams(db, project.id)
    labeling = LabelingTaskService(db)
    await labeling.delete_project_annotation_comments(project.id)
    await FeedbackService(db).delete_project_feedbacks(project.id)
    await labeling.delete_project_annotation_drafts(project.id)
    await labeling.delete_project_prediction_metadata(project.id)
    await labeling.clear_project_annotation_parents(project.id)
    await labeling.delete_project_annotations_and_predictions(project.id)
    await labeling.delete_project_task_locks(project.id)
    await BugReportService(db).detach_project_references(project.id)
    await labeling.delete_project_tasks(project.id)
    await delete_project_record(db, project)
    await db.flush()
