from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.dataset import Dataset, DatasetItem, ProjectDataset
from app.db.models.prediction import FailedPrediction, Prediction, PredictionMeta
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_dataset_item_link import TaskDatasetItemLink
from app.db.models.task_lock import AnnotationDraft, TaskLock
from app.db.models.user import User


_SQL_ARTIFACT = (
    Path(__file__).parents[1] / "alembic" / "sql" / "0202_dataset_unlink_labeling.sql"
)
_SCHEMA_ARTIFACT = (
    Path(__file__).parents[1]
    / "alembic"
    / "sql"
    / "0202_dataset_unlink_labeling.schema.json"
)


def _new_project(owner_id: uuid.UUID) -> Project:
    suffix = uuid.uuid4().hex[:10]
    return Project(
        id=uuid.uuid4(),
        display_id=f"DU-{suffix}",
        name="Dataset unlink fixture",
        type_label="图像检测",
        type_key="image-det",
        data_type="image",
        owner_id=owner_id,
    )


def _new_dataset(owner_id: uuid.UUID, name: str) -> Dataset:
    suffix = uuid.uuid4().hex[:10]
    return Dataset(
        id=uuid.uuid4(),
        display_id=f"DU-{suffix}",
        name=name,
        data_type="image",
        created_by=owner_id,
    )


def _new_task(
    project_id: uuid.UUID,
    *,
    file_name: str,
    dataset_item_id: uuid.UUID | None,
    status: str = "pending",
    batch_id: uuid.UUID | None = None,
) -> Task:
    return Task(
        id=uuid.uuid4(),
        project_id=project_id,
        dataset_item_id=dataset_item_id,
        display_id=f"DU-T-{uuid.uuid4().hex[:10]}",
        file_name=file_name,
        file_path=f"dataset-unlink/{file_name}",
        file_type="image",
        status=status,
        batch_id=batch_id,
    )


def _new_annotation(task: Task, project_id: uuid.UUID) -> Annotation:
    return Annotation(
        id=uuid.uuid4(),
        project_id=project_id,
        task_id=task.id,
        class_name="object",
        geometry={"type": "bbox", "x": 0, "y": 0, "width": 1, "height": 1},
    )


async def _scalar_json(db: AsyncSession, function: str, params: dict) -> dict:
    return (
        await db.execute(
            text(
                f"SELECT {function}(CAST(:dataset_id AS uuid), CAST(:project_id AS uuid))"
            ),
            params,
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_dataset_unlink_uses_primary_sources_and_recounts_owned_projections(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    dataset = _new_dataset(owner.id, "Primary source dataset")
    other_dataset = _new_dataset(owner.id, "Secondary source dataset")
    project = _new_project(owner.id)
    db_session.add_all([dataset, other_dataset, project])
    await db_session.flush()

    primary_item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="frame.pcd",
        file_path="dataset-unlink/frame.pcd",
        file_type="point_cloud",
    )
    camera_item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="camera.jpg",
        file_path="dataset-unlink/camera.jpg",
        file_type="image",
    )
    other_primary_item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=other_dataset.id,
        file_name="other.pcd",
        file_path="dataset-unlink/other.pcd",
        file_type="point_cloud",
    )
    default_batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id="B-DEFAULT",
        name="Default",
        status="draft",
        assigned_user_ids=[],
    )
    deleted_batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"DU-B-{uuid.uuid4().hex[:8]}",
        name="Dataset batch",
        status="draft",
        assigned_user_ids=[],
    )
    retained_batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"DU-B-{uuid.uuid4().hex[:8]}",
        name="Unrelated batch",
        status="reviewing",
        annotator_id=owner.id,
        assigned_user_ids=[str(owner.id)],
    )
    db_session.add_all(
        [
            primary_item,
            camera_item,
            other_primary_item,
            default_batch,
            deleted_batch,
            retained_batch,
        ]
    )
    await db_session.flush()

    direct_primary = _new_task(
        project.id,
        file_name="frame.pcd",
        dataset_item_id=primary_item.id,
        status="in_progress",
        batch_id=deleted_batch.id,
    )
    legacy_primary = _new_task(
        project.id,
        file_name="legacy-frame.pcd",
        dataset_item_id=None,
        status="completed",
        batch_id=default_batch.id,
    )
    legacy_primary.file_type = "point_cloud"
    camera_only = _new_task(
        project.id,
        file_name="camera-owner.pcd",
        dataset_item_id=other_primary_item.id,
        status="in_progress",
        batch_id=retained_batch.id,
    )
    source_less = _new_task(
        project.id,
        file_name="source-less.jpg",
        dataset_item_id=None,
    )
    direct_source_wins = _new_task(
        project.id,
        file_name="other-primary.pcd",
        dataset_item_id=other_primary_item.id,
    )
    db_session.add_all(
        [direct_primary, legacy_primary, camera_only, source_less, direct_source_wins]
    )
    db_session.add(ProjectDataset(dataset_id=dataset.id, project_id=project.id))
    await db_session.flush()

    db_session.add_all(
        [
            TaskDatasetItemLink(
                id=uuid.uuid4(),
                task_id=legacy_primary.id,
                dataset_item_id=primary_item.id,
                role="primary_lidar",
            ),
            TaskDatasetItemLink(
                id=uuid.uuid4(),
                task_id=camera_only.id,
                dataset_item_id=camera_item.id,
                role="camera_front",
                sensor_name="front",
            ),
            TaskDatasetItemLink(
                id=uuid.uuid4(),
                task_id=direct_source_wins.id,
                dataset_item_id=primary_item.id,
                role="primary_lidar",
            ),
        ]
    )
    target_annotations = [
        _new_annotation(direct_primary, project.id),
        _new_annotation(legacy_primary, project.id),
    ]
    db_session.add_all(target_annotations)
    await db_session.flush()

    prediction = Prediction(
        id=uuid.uuid4(),
        task_id=direct_primary.id,
        project_id=project.id,
        result={"shapes": []},
    )
    failed_prediction = FailedPrediction(
        id=uuid.uuid4(),
        task_id=direct_primary.id,
        project_id=project.id,
        error_type="test",
        message="test failure",
    )
    db_session.add_all([prediction, failed_prediction])
    await db_session.flush()
    db_session.add_all(
        [
            PredictionMeta(
                id=uuid.uuid4(),
                prediction_id=prediction.id,
                prediction_created_at=prediction.created_at,
                extra={},
            ),
            PredictionMeta(
                id=uuid.uuid4(),
                failed_prediction_id=failed_prediction.id,
                extra={},
            ),
            AnnotationComment(
                id=uuid.uuid4(),
                annotation_id=target_annotations[0].id,
                project_id=project.id,
                author_id=owner.id,
                body="remove with the task annotation",
            ),
            AnnotationDraft(
                id=uuid.uuid4(),
                task_id=direct_primary.id,
                annotation_id=target_annotations[0].id,
                user_id=owner.id,
                result={"geometry": {}},
            ),
            TaskLock(
                id=uuid.uuid4(),
                task_id=direct_primary.id,
                user_id=owner.id,
                expire_at=datetime.now(UTC) + timedelta(minutes=5),
                unique_id=uuid.uuid4(),
            ),
        ]
    )
    await db_session.flush()

    params = {"dataset_id": dataset.id, "project_id": project.id}
    task_ids = set(
        (
            await db_session.execute(
                text(
                    "SELECT task_id FROM aap_labeling_v1.dataset_task_references("
                    "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid))"
                ),
                params,
            )
        )
        .scalars()
        .all()
    )
    assert task_ids == {direct_primary.id, legacy_primary.id}

    preview = await _scalar_json(
        db_session, "aap_labeling_v1.preview_dataset_unlink", params
    )
    assert preview == {
        "schema": "aap-dataset-unlink-preview/v1",
        "ok": True,
        "data": {
            "will_delete_tasks": 2,
            "will_delete_annotations": 2,
            "will_delete_batches": 1,
        },
    }

    deleted = await _scalar_json(
        db_session, "aap_labeling_v1.delete_dataset_tasks", params
    )
    assert deleted["schema"] == "aap-dataset-task-unlink/v1"
    assert deleted["ok"] is True
    assert deleted["data"]["deleted_tasks"] == 2
    assert deleted["data"]["deleted_annotations"] == 2
    assert deleted["data"]["deleted_batches"] == 1
    assert deleted["data"]["deleted_batch_ids"] == [str(deleted_batch.id)]
    assert deleted["data"]["soft"] is False

    remaining_ids = set(
        (await db_session.execute(select(Task.id).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert remaining_ids == {
        camera_only.id,
        source_less.id,
        direct_source_wins.id,
    }
    remaining_batch_ids = set(
        (
            await db_session.execute(
                select(TaskBatch.id).where(TaskBatch.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert deleted_batch.id not in remaining_batch_ids
    assert remaining_batch_ids == {default_batch.id, retained_batch.id}
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AnnotationComment)
            .where(
                AnnotationComment.annotation_id.in_(
                    [annotation.id for annotation in target_annotations]
                )
            )
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AnnotationDraft)
            .where(AnnotationDraft.task_id == direct_primary.id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PredictionMeta)
            .where(PredictionMeta.prediction_id == prediction.id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(Prediction)
            .where(Prediction.id == prediction.id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(FailedPrediction)
            .where(FailedPrediction.id == failed_prediction.id)
        )
        == 0
    )

    refreshed = await db_session.get(Project, project.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.total_tasks == 3
    assert refreshed.completed_tasks == 0
    assert refreshed.review_tasks == 0
    assert refreshed.in_progress_tasks == 1
    assert refreshed.batch_summary == {"total": 2, "assigned": 1, "in_review": 1}
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(DatasetItem)
            .where(DatasetItem.id.in_([primary_item.id, camera_item.id]))
        )
        == 2
    )
    assert (
        await db_session.scalar(
            select(ProjectDataset.id).where(
                ProjectDataset.dataset_id == dataset.id,
                ProjectDataset.project_id == project.id,
            )
        )
        is not None
    )


@pytest.mark.asyncio
async def test_unlink_guard_retains_authored_task_feedback(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    dataset = _new_dataset(owner.id, "Feedback-retained dataset")
    project = _new_project(owner.id)
    db_session.add_all([dataset, project])
    await db_session.flush()
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="feedback.jpg",
        file_path="dataset-unlink/feedback.jpg",
    )
    task = _new_task(project.id, file_name=item.file_name, dataset_item_id=item.id)
    db_session.add_all([item, task])
    await db_session.flush()
    feedback = AnnotationFeedback(
        id=uuid.uuid4(),
        kind="comment",
        anchor_type="task",
        project_id=project.id,
        task_id=task.id,
        body="Retain this authored feedback",
        author_id=owner.id,
    )
    db_session.add(feedback)
    await db_session.flush()

    result = await _scalar_json(
        db_session,
        "aap_labeling_v1.guard_dataset_unlink",
        {"dataset_id": dataset.id, "project_id": project.id},
    )
    assert result == {
        "schema": "aap-source-retention/v1",
        "ok": False,
        "error": {"code": "task_feedback_retained"},
    }
    assert await db_session.get(Task, task.id) is not None
    assert await db_session.get(AnnotationFeedback, feedback.id) is not None


@pytest.mark.asyncio
async def test_unlink_guard_retains_outside_task_annotation_lineage(
    db_session: AsyncSession, super_admin
):
    owner, _ = super_admin
    dataset = _new_dataset(owner.id, "Annotation-lineage dataset")
    other_dataset = _new_dataset(owner.id, "Other task dataset")
    project = _new_project(owner.id)
    db_session.add_all([dataset, other_dataset, project])
    await db_session.flush()
    target_item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="parent.jpg",
        file_path="dataset-unlink/parent.jpg",
    )
    other_item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=other_dataset.id,
        file_name="child.jpg",
        file_path="dataset-unlink/child.jpg",
    )
    target_task = _new_task(
        project.id, file_name=target_item.file_name, dataset_item_id=target_item.id
    )
    other_task = _new_task(
        project.id, file_name=other_item.file_name, dataset_item_id=other_item.id
    )
    db_session.add_all([target_item, other_item, target_task, other_task])
    await db_session.flush()
    parent = _new_annotation(target_task, project.id)
    child = _new_annotation(other_task, project.id)
    child.parent_annotation_id = parent.id
    db_session.add_all([parent, child])
    await db_session.flush()

    result = await _scalar_json(
        db_session,
        "aap_labeling_v1.guard_dataset_unlink",
        {"dataset_id": dataset.id, "project_id": project.id},
    )
    assert result == {
        "schema": "aap-source-retention/v1",
        "ok": False,
        "error": {"code": "annotation_reference_retained"},
    }
    assert await db_session.get(Task, target_task.id) is not None
    assert await db_session.get(Annotation, parent.id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("locked_table", ["task_batches", "tasks"])
async def test_unlink_scope_fails_fast_when_batch_or_target_task_is_locked(
    test_engine, locked_table: str
):
    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    owner = User(
        id=uuid.uuid4(),
        email=f"dataset-unlink-{suffix}@test.local",
        name="Dataset unlink lock fixture",
        password_hash="test-only",
        role="super_admin",
        status="offline",
        is_active=True,
    )
    dataset = _new_dataset(owner.id, "Lock contention dataset")
    project = _new_project(owner.id)
    item = DatasetItem(
        id=uuid.uuid4(),
        dataset_id=dataset.id,
        file_name="locked.jpg",
        file_path=f"dataset-unlink/{suffix}.jpg",
    )
    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"DU-B-{suffix[:8]}",
        name="Locked batch",
        assigned_user_ids=[],
    )
    task = _new_task(
        project.id,
        file_name=item.file_name,
        dataset_item_id=item.id,
        batch_id=batch.id,
    )
    async with maker.begin() as seed:
        seed.add_all([owner, dataset, project])
        await seed.flush()
        seed.add_all([item, batch, task])
        seed.add(ProjectDataset(dataset_id=dataset.id, project_id=project.id))

    holder = maker()
    waiter = maker()
    try:
        row_id = batch.id if locked_table == "task_batches" else task.id
        await holder.execute(
            text(f"SELECT id FROM public.{locked_table} WHERE id = :id FOR UPDATE"),
            {"id": row_id},
        )
        with pytest.raises(DBAPIError):
            await asyncio.wait_for(
                waiter.execute(
                    text(
                        "SELECT aap_labeling_v1.lock_dataset_unlink_scope("
                        "CAST(:dataset_id AS uuid), CAST(:project_id AS uuid))"
                    ),
                    {"dataset_id": dataset.id, "project_id": project.id},
                ),
                timeout=3,
            )
    finally:
        await waiter.rollback()
        await holder.rollback()
        await waiter.close()
        await holder.close()
        async with maker.begin() as cleanup:
            await cleanup.execute(delete(Task).where(Task.project_id == project.id))
            await cleanup.execute(
                delete(TaskBatch).where(TaskBatch.project_id == project.id)
            )
            await cleanup.execute(
                delete(ProjectDataset).where(ProjectDataset.dataset_id == dataset.id)
            )
            await cleanup.execute(delete(Project).where(Project.id == project.id))
            await cleanup.execute(delete(Dataset).where(Dataset.id == dataset.id))
            await cleanup.execute(delete(User).where(User.id == owner.id))


def test_dataset_unlink_contract_schema_and_sql_are_frozen():
    schema = json.loads(_SCHEMA_ARTIFACT.read_text(encoding="utf-8"))
    arguments = schema["$defs"]["arguments"]
    assert arguments["additionalProperties"] is False
    assert arguments["required"] == ["p_dataset_id", "p_project_id"]
    assert schema["$defs"]["deleteData"]["additionalProperties"] is False
    assert set(schema["$defs"]["deleteData"]["required"]) == {
        "deleted_tasks",
        "deleted_annotations",
        "deleted_batches",
        "deleted_batch_ids",
        "soft",
    }

    artifact = _SQL_ARTIFACT.read_text(encoding="utf-8")
    statements = [
        statement.strip()
        for statement in artifact.split("-- aap-statement-break")
        if statement.strip()
    ]
    function_statements = statements[:6]
    assert len(function_statements) == 6
    assert all(
        "CREATE FUNCTION aap_labeling_v1." in statement
        for statement in function_statements
    )
    assert all(
        "search_path = pg_catalog, pg_temp" in statement
        for statement in function_statements
    )
    assert all(statement.endswith("$function$;") for statement in function_statements)
    assert all(
        statement.startswith("REVOKE ALL ON FUNCTION") for statement in statements[6:]
    )

    lock_scope = next(
        statement
        for statement in function_statements
        if "FUNCTION aap_labeling_v1.lock_dataset_unlink_scope" in statement
    )
    assert lock_scope.index("public.task_batches") < lock_scope.index("public.tasks")
    assert lock_scope.count("FOR UPDATE NOWAIT") == 2

    selector = function_statements[0]
    assert "task.dataset_item_id IS NULL" in selector
    assert "item_link.role = 'primary_lidar'" in selector
    assert "public.dataset_items" not in artifact
    assert "public.bug_reports" not in artifact
    assert "public.image_pyramid_assets" not in artifact
    assert "public.async_jobs" not in artifact
    assert re.search(r"dataset_task_references\(uuid, uuid\)", artifact)
