from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.annotation import Annotation
from app.db.models.annotation_comment import AnnotationComment
from app.db.models.annotation_feedback import AnnotationFeedback
from app.db.models.project import Project
from app.db.models.task import Task
from app.db.models.task_batch import TaskBatch
from app.db.models.task_event import TaskEvent
from app.db.models.task_lock import AnnotationDraft, TaskLock
from tests.test_asset_storage_cleanup_runtime import _verify_postgres


_SQL_ARTIFACT = (
    Path(__file__).parents[1] / "alembic" / "sql" / "0197_project_labeling_delete.sql"
)
_SCHEMA_ARTIFACT = (
    Path(__file__).parents[1]
    / "alembic"
    / "sql"
    / "0197_project_labeling_delete.schema.json"
)


def _new_project(owner_id: uuid.UUID, name: str) -> Project:
    suffix = uuid.uuid4().hex[:10]
    return Project(
        id=uuid.uuid4(),
        display_id=f"PD-{suffix}",
        name=name,
        type_label="图像检测",
        type_key="image-det",
        data_type="image",
        owner_id=owner_id,
    )


def _new_task(project_id: uuid.UUID, index: int) -> Task:
    suffix = uuid.uuid4().hex[:10]
    return Task(
        id=uuid.uuid4(),
        project_id=project_id,
        display_id=f"TD-{suffix}",
        file_name=f"image-{index}.jpg",
        file_path=f"project/{suffix}/image-{index}.jpg",
        file_type="image",
    )


async def _delete_owned_records(db: AsyncSession, project_id: uuid.UUID) -> dict:
    return (
        await db.execute(
            text("SELECT aap_labeling_v1.delete_project_owned_records(:project_id)"),
            {"project_id": project_id},
        )
    ).scalar_one()


@pytest.mark.asyncio
async def test_project_labeling_delete_cleans_owned_children_and_uses_outer_transaction(
    db_session: AsyncSession, super_admin, test_engine
):
    if os.environ.get("AAP_MODULE_DATA_ROLE_ACCEPTANCE") != "1":
        pytest.skip("legacy cross-project lineage requires the owned verifier")
    _verify_postgres(test_engine.url)
    owner, _ = super_admin
    project = _new_project(owner.id, "Labeling deletion target")
    other_project = _new_project(owner.id, "Unrelated project")
    db_session.add_all([project, other_project])
    await db_session.flush()

    batch = TaskBatch(
        id=uuid.uuid4(),
        project_id=project.id,
        display_id=f"BT-{uuid.uuid4().hex[:8]}",
        name="Target batch",
    )
    target_tasks = [_new_task(project.id, index) for index in range(2)]
    other_task = _new_task(other_project.id, 2)
    db_session.add_all([batch, *target_tasks, other_task])
    await db_session.flush()
    target_tasks[0].batch_id = batch.id

    target_annotation = Annotation(
        id=uuid.uuid4(),
        project_id=project.id,
        task_id=target_tasks[0].id,
        class_name="object",
        geometry={"type": "bbox", "x": 0, "y": 0, "width": 1, "height": 1},
    )
    other_annotation = Annotation(
        id=uuid.uuid4(),
        project_id=other_project.id,
        task_id=other_task.id,
        class_name="object",
        geometry={"type": "bbox", "x": 0, "y": 0, "width": 1, "height": 1},
        parent_annotation_id=target_annotation.id,
    )
    db_session.add(target_annotation)
    await db_session.flush()
    # Seed historical lineage without weakening the live parent-input guards.
    lineage_guards = (
        "trg_inherit_annotation_input_sources",
        "trg_inherit_annotation_video_input_sources",
    )
    async with db_session.begin_nested():
        for guard in lineage_guards:
            await db_session.execute(
                text(f"ALTER TABLE public.annotations DISABLE TRIGGER {guard}")
            )
        db_session.add(other_annotation)
        await db_session.flush()
        for guard in lineage_guards:
            await db_session.execute(
                text(f"ALTER TABLE public.annotations ENABLE TRIGGER {guard}")
            )
    assert (
        await db_session.execute(
            text(
                "SELECT tgname,tgenabled::text FROM pg_trigger WHERE tgrelid='public.annotations'::regclass AND tgname=ANY(CAST(:names AS text[])) ORDER BY tgname"
            ),
            {"names": list(lineage_guards)},
        )
    ).all() == [(guard, "O") for guard in sorted(lineage_guards)]
    with pytest.raises(DBAPIError) as rejected:
        async with db_session.begin_nested():
            db_session.add(
                Annotation(
                    id=uuid.uuid4(),
                    project_id=other_project.id,
                    task_id=other_task.id,
                    class_name="object",
                    geometry={"type": "bbox", "x": 0, "y": 0, "width": 1, "height": 1},
                    parent_annotation_id=target_annotation.id,
                )
            )
            await db_session.flush()
    assert getattr(rejected.value.orig, "sqlstate", None) == "23514"

    target_feedback = AnnotationFeedback(
        id=uuid.uuid4(),
        kind="issue",
        anchor_type="project",
        project_id=project.id,
        body="Target feedback",
        author_id=owner.id,
    )
    db_session.add(target_feedback)
    await db_session.flush()
    other_feedback = AnnotationFeedback(
        id=uuid.uuid4(),
        kind="comment",
        anchor_type="task",
        project_id=other_project.id,
        task_id=other_task.id,
        body="Unrelated thread reply",
        author_id=owner.id,
        thread_parent_id=target_feedback.id,
    )
    db_session.add_all(
        [
            AnnotationComment(
                id=uuid.uuid4(),
                annotation_id=target_annotation.id,
                project_id=None,
                author_id=owner.id,
                body="Comment without project denormalization",
            ),
            other_feedback,
            AnnotationDraft(
                id=uuid.uuid4(),
                task_id=target_tasks[0].id,
                annotation_id=target_annotation.id,
                user_id=owner.id,
                result={"geometry": {}},
            ),
            TaskLock(
                id=uuid.uuid4(),
                task_id=target_tasks[0].id,
                user_id=owner.id,
                expire_at=datetime.now(UTC),
                unique_id=uuid.uuid4(),
            ),
            TaskEvent(
                id=uuid.uuid4(),
                task_id=target_tasks[0].id,
                user_id=owner.id,
                project_id=project.id,
                kind="annotate",
                started_at=datetime.now(UTC),
                ended_at=datetime.now(UTC),
                duration_ms=0,
            ),
        ]
    )
    await db_session.flush()

    task_refs = list(
        (
            await db_session.execute(
                text(
                    "SELECT task_id FROM aap_labeling_v1.project_task_references(:project_id)"
                ),
                {"project_id": project.id},
            )
        ).scalars()
    )
    assert task_refs == sorted(task.id for task in target_tasks)

    # The capability participates in the caller transaction: rolling back its
    # work must restore every row and make the frozen result disappear.
    savepoint = await db_session.begin_nested()
    rolled_back_result = await _delete_owned_records(db_session, project.id)
    assert rolled_back_result == {
        "schema": "aap-project-labeling-delete/v1",
        "ok": True,
        "data": {"deleted_tasks": 2, "deleted_annotations": 1},
    }
    await savepoint.rollback()
    assert (
        await db_session.scalar(
            select(func.count()).select_from(Task).where(Task.project_id == project.id)
        )
        == 2
    )

    result = await _delete_owned_records(db_session, project.id)
    assert result == rolled_back_result
    assert await db_session.get(Project, project.id) is not None
    assert await db_session.get(Task, other_task.id) is not None
    await db_session.refresh(other_annotation)
    assert other_annotation.parent_annotation_id is None
    assert (
        await db_session.scalar(
            select(AnnotationFeedback.id).where(
                AnnotationFeedback.id == target_feedback.id
            )
        )
        is None
    )
    await db_session.refresh(other_feedback)
    assert other_feedback.thread_parent_id is None
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TaskBatch)
            .where(TaskBatch.project_id == project.id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AnnotationComment)
            .where(AnnotationComment.annotation_id == target_annotation.id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(AnnotationDraft)
            .where(AnnotationDraft.task_id.in_([task.id for task in target_tasks]))
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TaskLock)
            .where(TaskLock.task_id.in_([task.id for task in target_tasks]))
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(TaskEvent)
            .where(TaskEvent.project_id == project.id)
        )
        == 0
    )


def test_project_labeling_delete_schema_and_sql_freeze_the_public_contract():
    schema = json.loads(_SCHEMA_ARTIFACT.read_text(encoding="utf-8"))
    request = schema["$defs"]["projectRequest"]
    response = schema["$defs"]["deleteResponse"]
    data = schema["$defs"]["deleteData"]
    task_refs = schema["$defs"]["taskReferences"]

    assert request["additionalProperties"] is False
    assert request["required"] == ["project_id"]
    assert response["additionalProperties"] is False
    assert response["properties"]["schema"]["const"] == (
        "aap-project-labeling-delete/v1"
    )
    assert data["additionalProperties"] is False
    assert set(data["required"]) == {"deleted_tasks", "deleted_annotations"}
    item_name = task_refs["items"]["$ref"].rsplit("/", 1)[-1]
    assert schema["$defs"][item_name]["additionalProperties"] is False

    artifact = _SQL_ARTIFACT.read_text(encoding="utf-8")
    statements = [
        statement.strip()
        for statement in artifact.split("-- aap-statement-break")
        if statement.strip()
    ]
    assert all(
        "CREATE FUNCTION" in statement and statement.endswith("$function$;")
        for statement in statements[:2]
    )
    assert all(
        "search_path = pg_catalog, pg_temp" in statement for statement in statements[:2]
    )
    assert "FOR UPDATE NOWAIT" in statements[1]
    assert all(statement.endswith(";") for statement in statements[2:])

    # Project-FK rows owned by Labeling must be removed by this owner function;
    # immutable creation/append receipts have no Project FK and are retained.
    delete_statement = statements[1].lower()
    owned_project_tables = {
        "tasks",
        "task_batches",
        "task_events",
        "annotations",
        "annotation_comments",
        "annotation_feedbacks",
        "predictions",
        "failed_predictions",
        "scene_tracks",
        "mask_annotation_revisions",
        "mask_format_imports",
        "mask_qc_runs",
        "mask_qc_issues",
        "mask_repair_batches",
        "mask_review_scopes",
        "point_cloud_quality_runs",
        "point_cloud_quality_issues",
        "point_cloud_quality_evaluations",
    }
    deleted_tables = set(
        re.findall(r"delete\s+from\s+public\.([a-z_]+)", delete_statement)
    )
    assert owned_project_tables <= deleted_tables
    assert "task_creation_operations" not in deleted_tables
    assert "task_creation_chunks" not in deleted_tables
    assert "task_append_operations" not in deleted_tables


@pytest.mark.asyncio
async def test_project_task_references_rejects_null_project_id(
    db_session: AsyncSession,
):
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                text(
                    "SELECT * FROM aap_labeling_v1.project_task_references(NULL::uuid)"
                )
            )


@pytest.mark.asyncio
async def test_project_labeling_delete_fails_fast_when_a_task_is_locked(test_engine):
    from app.db.models.user import User

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    user_id = uuid.uuid4()
    project = Project(
        id=uuid.uuid4(),
        display_id=f"PL-{suffix[:10]}",
        name="NOWAIT project deletion fixture",
        type_label="图像检测",
        type_key="image-det",
        data_type="image",
        owner_id=user_id,
    )
    task = _new_task(project.id, 0)
    async with maker() as seed:
        seed.add(
            User(
                id=user_id,
                email=f"project-delete-{suffix}@test.local",
                name="Project deletion fixture",
                password_hash="test-only",
                role="super_admin",
                status="offline",
                is_active=True,
            )
        )
        await seed.flush()
        seed.add(project)
        await seed.flush()
        seed.add(task)
        await seed.commit()

    locker, contender = maker(), maker()
    try:
        await locker.begin()
        await locker.execute(
            text("SELECT id FROM public.tasks WHERE id = :task_id FOR UPDATE"),
            {"task_id": task.id},
        )

        await contender.begin()
        with pytest.raises(DBAPIError) as busy:
            await _delete_owned_records(contender, project.id)
        assert getattr(busy.value.orig, "sqlstate", None) == "55P03"
        await contender.rollback()
    finally:
        await locker.rollback()
        await contender.rollback()
        async with maker.begin() as observer:
            assert await observer.get(Task, task.id) is not None
            assert await observer.get(Project, project.id) is not None
            await observer.execute(delete(Task).where(Task.id == task.id))
            await observer.execute(delete(Project).where(Project.id == project.id))
            await observer.execute(delete(User).where(User.id == user_id))
